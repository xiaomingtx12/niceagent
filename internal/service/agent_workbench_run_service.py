"""工作台 Run 摘要和协议事件的持久化服务。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from injector import inject
from sqlalchemy import and_, func, or_, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from internal.entity import (
    WORKBENCH_ACTIVE_STATUSES,
    WORKBENCH_TERMINAL_STATUSES,
    WorkbenchRun,
    WorkbenchRunStatus,
)
from internal.entity.conversation_entity import InvokeFrom
from internal.exception import NotFoundException
from internal.model import AgentRun, AgentRunEvent, Message
from internal.schema.agent_workbench_schema import (
    WorkbenchEvent,
    WorkbenchHistoryMessageResp,
    WorkbenchMessagePageInfoResp,
    WorkbenchMessagePageResp,
    WorkbenchRunMetadataResp,
    WorkbenchStateResp,
)
from pkg.sqlalchemy import SQLAlchemy

from .base_service import BaseService


_PERSISTED_EVENT_METHODS = ("tools", "lifecycle")


def _state_snapshot_event(
    run_id: UUID,
    sequence: int,
    snapshot: Mapping[str, Any] | None,
) -> WorkbenchEvent | None:
    """把 Run 最新快照压缩成一条历史水合事件。"""
    if not snapshot:
        return None
    data = {key: snapshot[key] for key in ("todos", "files") if key in snapshot}
    if not data:
        return None
    return WorkbenchEvent(
        type="event",
        event_id=f"{run_id}:state-snapshot",
        seq=max(0, sequence),
        method="values",
        params={
            "namespace": [],
            "timestamp": None,
            "data": data,
            "metadata": {
                "my_dify": {
                    "source": "state_hydration",
                    "run_id": str(run_id),
                }
            },
        },
    )


@inject
@dataclass
class AgentWorkbenchRunService(BaseService):
    """维护数据库中的 ``AgentRun``、``AgentRunEvent`` 和生命周期状态。

    ``AgentRun`` 是平台运行记录，不是 Agent Core 的执行对象；本服务不
    创建模型、工具或 LangGraph，只负责持久化和查询运行状态。
    """

    db: SQLAlchemy

    def lock_thread_start(self, thread_id: UUID) -> None:
        """在当前事务内串行化同一线程的 Run 创建。"""
        self.db.session.execute(
            text("SELECT pg_advisory_xact_lock(" "hashtextextended(:thread_id, 0))"),
            {"thread_id": str(thread_id)},
        )

    def add_run(self, run: WorkbenchRun) -> AgentRun:
        """在调用方事务中创建平台级 ``AgentRun`` 数据库记录。"""
        if run.spec.app_id is None:
            raise ValueError("工作台 Run 缺少 app_id")
        row = AgentRun(
            id=run.run_id,
            app_id=run.spec.app_id,
            thread_id=run.thread_id,
            invoke_from=run.spec.invoke_from.value,
            message_id=run.spec.message_id,
            input_message_id=run.spec.input_message_id,
            status=run.status.value,
            last_event_sequence=run.initial_event_sequence,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        self.db.session.add(row)
        self.db.session.flush()
        return row

    def set_status(
        self,
        run: WorkbenchRun,
        status: WorkbenchRunStatus,
        *,
        error_message: str = "",
    ) -> None:
        """更新 Run 状态和生命周期时间。"""
        row = self.db.session.get(AgentRun, run.run_id)
        if row is None:
            return
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.db.auto_commit():
            row.status = status.value
            row.error_message = error_message
            if status is WorkbenchRunStatus.RUNNING and row.started_at is None:
                row.started_at = now
            if status.value in WORKBENCH_TERMINAL_STATUSES:
                row.finished_at = now

    def finalize_run(
        self,
        run: WorkbenchRun,
        status: WorkbenchRunStatus,
        *,
        error_message: str = "",
        message_updates: Mapping[str, Any] | None = None,
    ) -> None:
        """在一个事务中收敛 Run 状态和对应业务消息。"""
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.db.auto_commit():
            row = self.db.session.get(AgentRun, run.run_id)
            if row is not None:
                row.status = status.value
                row.error_message = error_message
                row.finished_at = now
            if message_updates:
                message = self.db.session.get(Message, run.spec.message_id)
                if message is not None:
                    for field, value in message_updates.items():
                        setattr(message, field, value)

    def record_event(
        self,
        run: WorkbenchRun,
        event: WorkbenchEvent,
    ) -> None:
        """兼容单事件调用，并复用批量持久化实现。"""
        self.record_events(run, [event])

    def record_events(
        self,
        run: WorkbenchRun,
        events: list[WorkbenchEvent],
    ) -> None:
        """一次事务更新 Run 快照并追加一批需要历史恢复的事件。"""
        durable_events = [event for event in events if event.method != "messages"]
        if not durable_events:
            return

        latest_sequence = max(event.seq for event in durable_events)
        latest_snapshot = next(
            (
                event.params.data
                for event in reversed(durable_events)
                if event.method == "values" and isinstance(event.params.data, dict)
            ),
            None,
        )
        persisted_payloads = {
            event.seq: event.model_dump(mode="json")
            for event in durable_events
            if event.method in _PERSISTED_EVENT_METHODS
        }

        with self.db.auto_commit():
            row = self.db.session.get(AgentRun, run.run_id)
            if row is None:
                return
            row.last_event_sequence = max(
                int(row.last_event_sequence or 0),
                latest_sequence,
            )
            if latest_snapshot is not None:
                run.state_snapshot = latest_snapshot
                row.state_snapshot = latest_snapshot
            if persisted_payloads:
                statement = pg_insert(AgentRunEvent).values(
                    [
                        {
                            "run_id": run.run_id,
                            "sequence": sequence,
                            "payload": payload,
                        }
                        for sequence, payload in persisted_payloads.items()
                    ]
                )
                self.db.session.execute(
                    statement.on_conflict_do_nothing(
                        index_elements=["run_id", "sequence"]
                    )
                )

    def get_run(self, run_id: UUID) -> AgentRun | None:
        """按主键读取 Run。"""
        return self.db.session.get(AgentRun, run_id)

    def get_start_candidates(
        self,
        *,
        thread_id: UUID,
        input_message_id: str | None,
    ) -> tuple[AgentRun | None, AgentRun | None]:
        """一次查询取得幂等 Run 和当前活动 Run。"""
        candidate_filters = [AgentRun.status.in_(WORKBENCH_ACTIVE_STATUSES)]
        if input_message_id is not None:
            candidate_filters.append(AgentRun.input_message_id == input_message_id)
        rows = (
            self.db.session.query(AgentRun)
            .filter(
                AgentRun.thread_id == thread_id,
                or_(*candidate_filters),
            )
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .all()
        )
        duplicate_run = next(
            (
                row
                for row in rows
                if input_message_id is not None
                and row.input_message_id == input_message_id
            ),
            None,
        )
        active_run = next(
            (row for row in rows if row.status in WORKBENCH_ACTIVE_STATUSES),
            None,
        )
        return duplicate_run, active_run

    def get_latest_run(
        self,
        *,
        thread_id: UUID,
        invoke_from: InvokeFrom,
    ) -> AgentRun | None:
        """读取线程指定来源的最新 Run，并释放只读 Session。"""
        try:
            return (
                self.db.session.query(AgentRun)
                .populate_existing()
                .filter(
                    AgentRun.thread_id == thread_id,
                    AgentRun.invoke_from == invoke_from.value,
                )
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                .first()
            )
        finally:
            self.db.session.close()

    def get_thread_last_event_sequence(
        self,
        *,
        thread_id: UUID,
        invoke_from: InvokeFrom,
        close_session: bool = True,
    ) -> int:
        """读取线程已经持久化的最大事件序号。"""
        sequence = int(
            self.db.session.query(func.max(AgentRun.last_event_sequence))
            .filter(
                AgentRun.thread_id == thread_id,
                AgentRun.invoke_from == invoke_from.value,
            )
            .scalar()
            or 0
        )
        if close_session:
            self.db.session.close()
        return sequence

    def list_runs_for_messages(
        self,
        *,
        thread_id: UUID,
        invoke_from: InvokeFrom,
        message_ids: set[UUID],
    ) -> list[AgentRun]:
        """按创建顺序读取当前消息页关联的 Run。"""
        if not message_ids:
            return []
        try:
            return (
                self.db.session.query(AgentRun)
                .populate_existing()
                .filter(
                    AgentRun.thread_id == thread_id,
                    AgentRun.invoke_from == invoke_from.value,
                    AgentRun.message_id.in_(message_ids),
                )
                .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
                .all()
            )
        finally:
            self.db.session.close()

    def list_events(
        self,
        run_id: UUID,
        *,
        after_sequence: int = 0,
    ) -> list[WorkbenchEvent]:
        """按序号读取一个 Run 的持久化事件。"""
        try:
            rows = (
                self.db.session.query(AgentRunEvent)
                .filter(
                    AgentRunEvent.run_id == run_id,
                    AgentRunEvent.sequence > after_sequence,
                    AgentRunEvent.payload["method"].astext.in_(
                        _PERSISTED_EVENT_METHODS
                    ),
                )
                .order_by(AgentRunEvent.sequence.asc())
                .all()
            )
            return [WorkbenchEvent.model_validate(row.payload) for row in rows]
        finally:
            self.db.session.close()

    def list_events_by_run(
        self,
        run_ids: list[UUID],
        *,
        close_session: bool = True,
    ) -> dict[UUID, list[WorkbenchEvent]]:
        """批量读取多个 Run 的持久化事件。"""
        if not run_ids:
            return {}
        try:
            rows = (
                self.db.session.query(AgentRunEvent)
                .filter(
                    AgentRunEvent.run_id.in_(run_ids),
                    AgentRunEvent.payload["method"].astext.in_(
                        _PERSISTED_EVENT_METHODS
                    ),
                )
                .order_by(
                    AgentRunEvent.run_id.asc(),
                    AgentRunEvent.sequence.asc(),
                )
                .all()
            )
            events: dict[UUID, list[WorkbenchEvent]] = {
                run_id: [] for run_id in run_ids
            }
            for row in rows:
                events.setdefault(row.run_id, []).append(
                    WorkbenchEvent.model_validate(row.payload)
                )
            return events
        finally:
            if close_session:
                self.db.session.close()

    def is_cancel_requested(self, run_id: UUID) -> bool:
        """读取持久化取消请求，并立即释放 Session。"""
        try:
            row = (
                self.db.session.query(AgentRun)
                .populate_existing()
                .filter(AgentRun.id == run_id)
                .one_or_none()
            )
            return row is not None and row.status in {
                WorkbenchRunStatus.CANCELLING.value,
                WorkbenchRunStatus.CANCELLED.value,
            }
        finally:
            self.db.session.close()

    def has_unfinished_predecessor(self, run_id: UUID) -> bool:
        """判断当前 Run 之前是否还有未终止 Run。"""
        try:
            current = self.db.session.get(AgentRun, run_id)
            if current is None:
                return False
            predecessor = (
                self.db.session.query(AgentRun.id)
                .filter(
                    AgentRun.thread_id == current.thread_id,
                    AgentRun.id != current.id,
                    AgentRun.status.in_(WORKBENCH_ACTIVE_STATUSES),
                    or_(
                        AgentRun.created_at < current.created_at,
                        and_(
                            AgentRun.created_at == current.created_at,
                            AgentRun.id < current.id,
                        ),
                    ),
                )
                .first()
            )
            return predecessor is not None
        finally:
            self.db.session.close()

    def request_cancel(self, row: AgentRun) -> None:
        """持久化取消请求，真正结束后再进入 cancelled 终态。"""
        with self.db.auto_commit():
            row.status = WorkbenchRunStatus.CANCELLING.value

    def get_message_page(
        self,
        *,
        thread_id: UUID,
        before: UUID | None = None,
        page_size: int | None = None,
    ) -> WorkbenchMessagePageResp:
        """按业务消息游标读取一页对话。"""
        # Handler 已通过 CursorPaginatorReq 完成类型和范围校验；这里仅保留
        # Service 的默认值，避免再次把字符串强制转换成整数。
        limit = min(max(page_size if page_size is not None else 20, 1), 50)
        try:
            query = self.db.session.query(Message).filter(
                Message.conversation_id == thread_id,
                Message.is_deleted.is_(False),
            )
            if before is not None:
                cursor = (
                    self.db.session.query(Message)
                    .filter(
                        Message.id == before,
                        Message.conversation_id == thread_id,
                        Message.is_deleted.is_(False),
                    )
                    .one_or_none()
                )
                if cursor is None:
                    raise NotFoundException("历史消息游标不存在")
                query = query.filter(
                    or_(
                        Message.created_at < cursor.created_at,
                        and_(
                            Message.created_at == cursor.created_at,
                            Message.id < cursor.id,
                        ),
                    )
                )
            descending_rows = (
                query.order_by(Message.created_at.desc(), Message.id.desc())
                .limit(limit + 1)
                .all()
            )
            has_more = len(descending_rows) > limit
            page_rows = descending_rows[:limit]
            return WorkbenchMessagePageResp(
                messages=self._project_messages(list(reversed(page_rows))),
                page=WorkbenchMessagePageInfoResp(
                    has_more=has_more,
                    next_before=page_rows[-1].id if has_more and page_rows else None,
                ),
            )
        finally:
            self.db.session.close()

    def get_state(
        self,
        *,
        invoke_from: InvokeFrom,
        thread_id: UUID,
        active_run: WorkbenchRun | None,
    ) -> WorkbenchStateResp:
        """水合首屏消息、历史事件和最新状态快照。"""
        try:
            message_page, persisted_runs = self._get_state_message_page_and_runs(
                thread_id=thread_id,
                invoke_from=invoke_from,
            )
            latest_run = persisted_runs[-1] if persisted_runs else None
            source_snapshot = (
                active_run.state_snapshot
                if active_run is not None and active_run.state_snapshot is not None
                else (
                    latest_run.state_snapshot
                    if latest_run is not None and latest_run.state_snapshot is not None
                    else {}
                )
            )
            values = {
                key: source_snapshot[key]
                for key in ("todos", "files")
                if key in source_snapshot
            }
            values["messages"] = [
                message.model_dump(mode="json") for message in message_page.messages
            ]
            values["message_page"] = message_page.page.model_dump(mode="json")

            events_by_run = self.list_events_by_run(
                [run.id for run in persisted_runs],
                close_session=False,
            )
            historical_events: list[WorkbenchEvent] = []
            for run in persisted_runs:
                historical_events.extend(events_by_run.get(run.id, []))
                snapshot = (
                    active_run.state_snapshot
                    if active_run is not None and active_run.run_id == run.id
                    else run.state_snapshot
                )
                snapshot_event = _state_snapshot_event(
                    run.id,
                    int(run.last_event_sequence or 0),
                    snapshot,
                )
                if snapshot_event is not None:
                    historical_events.append(snapshot_event)
            known_event_ids = {event.event_id for event in historical_events}
            if active_run is not None:
                historical_events.extend(
                    event
                    for event in active_run.events
                    if event.method in {"tools", "lifecycle"}
                    and event.event_id not in known_event_ids
                )
                if all(run.id != active_run.run_id for run in persisted_runs):
                    snapshot_event = _state_snapshot_event(
                        active_run.run_id,
                        active_run.last_event_sequence,
                        active_run.state_snapshot,
                    )
                    if snapshot_event is not None:
                        historical_events.append(snapshot_event)
            values["workbench_events"] = [
                event.model_dump(mode="json")
                for event in sorted(historical_events, key=lambda item: item.seq)
            ]

            run_id = (
                active_run.run_id
                if active_run is not None
                else latest_run.id if latest_run else None
            )
            status = (
                active_run.status.value
                if active_run is not None
                else str(latest_run.status) if latest_run else "idle"
            )
            last_event_sequence = (
                active_run.last_event_sequence
                if active_run is not None
                else (
                    int(latest_run.last_event_sequence or 0)
                    if latest_run is not None
                    else 0
                )
            )
            return WorkbenchStateResp(
                values=values,
                next=["agent"] if status in {"queued", "running", "cancelling"} else [],
                tasks=[],
                metadata=WorkbenchRunMetadataResp(
                    run_id=run_id,
                    status=status,
                    last_event_sequence=last_event_sequence,
                ),
            )
        finally:
            self.db.session.close()

    def _get_state_message_page_and_runs(
        self,
        *,
        thread_id: UUID,
        invoke_from: InvokeFrom,
        page_size: int = 20,
    ) -> tuple[WorkbenchMessagePageResp, list[AgentRun]]:
        """用一次查询取得首屏消息及其关联 Run。"""
        limit = min(max(page_size, 1), 50)
        descending_rows = (
            self.db.session.query(Message, AgentRun)
            .outerjoin(
                AgentRun,
                and_(
                    AgentRun.message_id == Message.id,
                    AgentRun.thread_id == thread_id,
                ),
            )
            .filter(
                Message.conversation_id == thread_id,
                Message.is_deleted.is_(False),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit + 1)
            .all()
        )
        has_more = len(descending_rows) > limit
        page_rows = descending_rows[:limit]
        ascending_rows = list(reversed(page_rows))
        messages = self._project_messages([message for message, _ in ascending_rows])
        runs = [run for _, run in ascending_rows if run is not None]
        return (
            WorkbenchMessagePageResp(
                messages=messages,
                page=WorkbenchMessagePageInfoResp(
                    has_more=has_more,
                    next_before=(
                        page_rows[-1][0].id if has_more and page_rows else None
                    ),
                ),
            ),
            runs,
        )

    @staticmethod
    def _project_messages(rows: list[Message]) -> list[WorkbenchHistoryMessageResp]:
        """将业务消息投影为工作台消息结构。"""
        messages: list[WorkbenchHistoryMessageResp] = []
        for row in rows:
            messages.append(
                WorkbenchHistoryMessageResp(
                    id=f"{row.id}:human",
                    type="human",
                    content=row.query,
                    additional_kwargs={
                        "image_urls": list(row.image_urls or []),
                        "attachments": list(row.attachments or []),
                    },
                )
            )
            if row.answer:
                messages.append(
                    WorkbenchHistoryMessageResp(
                        id=f"{row.id}:assistant",
                        type="ai",
                        content=row.answer,
                        additional_kwargs={
                            "metrics": {
                                "latency": float(row.latency or 0),
                                "total_token_count": int(row.total_token_count or 0),
                                "input_token_count": int(row.input_token_count or 0),
                                "output_token_count": int(row.output_token_count or 0),
                                "cached_token_count": int(row.cached_token_count or 0),
                                "reasoning_token_count": int(
                                    row.reasoning_token_count or 0
                                ),
                            }
                        },
                    )
                )
        return messages


__all__ = ["AgentWorkbenchRunService"]
