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
from internal.core.agent_v2.protocol.event_projector import (
    AgentProtocolEventProjector,
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
_STATE_SNAPSHOT_KEYS = ("todos", "files", "async_tasks")


@inject
@dataclass
class AgentWorkbenchRunService(BaseService):
    """维护数据库中的 ``AgentRun``、``AgentRunEvent`` 和生命周期状态。

    ``AgentRun`` 是平台运行记录，不是 Agent Core 的执行对象；本服务不
    创建模型、工具或 LangGraph，只负责持久化和查询运行状态。
    """

    db: SQLAlchemy

    # -------------------- Public persistence and query API --------------------

    def lock_conversation_start(self, conversation_id: UUID) -> None:
        """在当前事务内串行化同一会话的 Run 创建。"""
        self.db.session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(:conversation_id, 0))"
            ),
            {"conversation_id": str(conversation_id)},
        )

    def add_run(self, run: WorkbenchRun) -> AgentRun:
        """在调用方事务中创建平台级 ``AgentRun`` 数据库记录。"""
        # 1.校验运行上下文，建立平台 Run 摘要。
        if run.spec.app_id is None:
            raise ValueError("工作台 Run 缺少 app_id")
        row = AgentRun(
            id=run.run_id,
            app_id=run.spec.app_id,
            conversation_id=run.conversation_id,
            invoke_from=run.spec.invoke_from.value,
            message_id=run.spec.message_id,
            input_message_id=run.spec.input_message_id,
            status=run.status.value,
            last_event_sequence=run.initial_event_sequence,
            created_at=datetime.now(UTC).replace(tzinfo=None),
        )
        # 2.交给调用方事务 flush，保证后续事件可以按 Run 主键写入。
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
        # 1.在事务内锁定当前 Run，避免并发执行路径覆盖已落库终态。
        with self.db.auto_commit():
            row = (
                self.db.session.query(AgentRun)
                .with_for_update()
                .filter(AgentRun.id == run.run_id)
                .one_or_none()
            )
            if row is None:
                return
            if (
                row.status in WORKBENCH_TERMINAL_STATUSES
                and row.status != status.value
            ):
                return
            if (
                row.status == WorkbenchRunStatus.CANCELLING.value
                and status is WorkbenchRunStatus.RUNNING
            ):
                return
            now = datetime.now(UTC).replace(tzinfo=None)
            row.status = status.value
            row.error_message = error_message
            if status is WorkbenchRunStatus.RUNNING and row.started_at is None:
                row.started_at = now
            if status.value in WORKBENCH_TERMINAL_STATUSES and row.finished_at is None:
                row.finished_at = now

    def finalize_run(
        self,
        run: WorkbenchRun,
        status: WorkbenchRunStatus,
        *,
        error_message: str = "",
        message_updates: Mapping[str, Any] | None = None,
        observability: Mapping[str, Any] | None = None,
    ) -> None:
        """在一个事务中收敛 Run、业务消息和平台观测快照。"""
        # 1.在事务内锁定并更新 Run 终态、观测快照和结束时间。
        with self.db.auto_commit():
            row = (
                self.db.session.query(AgentRun)
                .with_for_update()
                .filter(AgentRun.id == run.run_id)
                .one_or_none()
            )
            can_finalize = row is not None and (
                row.status not in WORKBENCH_TERMINAL_STATUSES
                or row.status == status.value
            )
            if row is not None:
                if can_finalize:
                    now = datetime.now(UTC).replace(tzinfo=None)
                    row.status = status.value
                    row.error_message = error_message
                    if row.finished_at is None:
                        row.finished_at = now
                    if observability is not None:
                        row.observability = dict(observability)
            # 2.只有终态收敛成功时才同步业务消息，避免并发覆盖。
            if can_finalize and message_updates:
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
        # 1.过滤消息增量，提取本批次的最新游标、快照和持久化事件。
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

        # 2.在一个事务中更新 Run 快照并幂等追加事件。
        with self.db.auto_commit():
            row = (
                self.db.session.query(AgentRun)
                .with_for_update()
                .filter(AgentRun.id == run.run_id)
                .one_or_none()
            )
            if row is None:
                return
            previous_sequence = int(row.last_event_sequence or 0)
            row.last_event_sequence = max(
                previous_sequence,
                latest_sequence,
            )
            if latest_snapshot is not None and latest_sequence >= previous_sequence:
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
        """按主键读取 Run；调用方若继续使用 ORM 对象需负责关闭 Session。"""
        return self.db.session.get(AgentRun, run_id)

    def get_run_progress(self, run_id: UUID) -> WorkbenchRunMetadataResp | None:
        """读取流跟随所需的 Run 状态和事件序号，并立即释放 Session。"""
        try:
            # 1.只选择流服务需要的状态和最新序号，避免加载完整 ORM 实体。
            row = (
                self.db.session.query(
                    AgentRun.status,
                    AgentRun.last_event_sequence,
                )
                .filter(AgentRun.id == run_id)
                .one_or_none()
            )
            if row is None:
                return None

            # 2.转换成具名响应对象，并在 finally 中释放短查询 Session。
            return WorkbenchRunMetadataResp(
                run_id=run_id,
                status=str(row.status),
                last_event_sequence=int(row.last_event_sequence or 0),
            )
        finally:
            self.db.session.close()

    def get_start_candidates(
        self,
        *,
        conversation_id: UUID,
        input_message_id: str | None,
    ) -> tuple[AgentRun | None, AgentRun | None]:
        """一次查询取得幂等 Run 和当前活动 Run。"""
        # 1.查询同会话的幂等候选和活动候选。
        candidate_filters = [AgentRun.status.in_(WORKBENCH_ACTIVE_STATUSES)]
        if input_message_id is not None:
            candidate_filters.append(AgentRun.input_message_id == input_message_id)
        rows = (
            self.db.session.query(AgentRun)
            .filter(
                AgentRun.conversation_id == conversation_id,
                or_(*candidate_filters),
            )
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .all()
        )
        # 2.按输入消息和运行状态分别选择结果，避免重复提交创建新 Run。
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
        conversation_id: UUID,
        invoke_from: InvokeFrom,
    ) -> AgentRun | None:
        """读取会话指定来源的最新 Run，并释放只读 Session。"""
        try:
            return (
                self.db.session.query(AgentRun)
                .populate_existing()
                .filter(
                    AgentRun.conversation_id == conversation_id,
                    AgentRun.invoke_from == invoke_from.value,
                )
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                .first()
            )
        finally:
            self.db.session.close()

    def get_conversation_last_event_sequence(
        self,
        *,
        conversation_id: UUID,
        invoke_from: InvokeFrom,
        close_session: bool = True,
    ) -> int:
        """读取会话已经持久化的最大事件序号。"""
        try:
            return int(
                self.db.session.query(func.max(AgentRun.last_event_sequence))
                .filter(
                    AgentRun.conversation_id == conversation_id,
                    AgentRun.invoke_from == invoke_from.value,
                )
                .scalar()
                or 0
            )
        finally:
            if close_session:
                self.db.session.close()

    def list_runs_for_messages(
        self,
        *,
        conversation_id: UUID,
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
                    AgentRun.conversation_id == conversation_id,
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
            return [
                visible
                for row in rows
                if (visible := AgentProtocolEventProjector.to_public_event(row.payload))
                is not None
            ]
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
                visible = AgentProtocolEventProjector.to_public_event(row.payload)
                if visible is not None:
                    events.setdefault(row.run_id, []).append(visible)
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
                    AgentRun.conversation_id == current.conversation_id,
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

    def request_cancel(self, row: AgentRun) -> bool:
        """仅把活动 Run 标记为 cancelling，避免并发请求复活终态 Run。"""
        updated = False
        with self.db.auto_commit():
            current = (
                self.db.session.query(AgentRun)
                .with_for_update()
                .filter(
                    AgentRun.id == row.id,
                    AgentRun.status.in_(WORKBENCH_ACTIVE_STATUSES),
                )
                .one_or_none()
            )
            if current is not None:
                current.status = WorkbenchRunStatus.CANCELLING.value
                updated = True
            else:
                latest_status = (
                    self.db.session.query(AgentRun.status)
                    .filter(AgentRun.id == row.id)
                    .scalar()
                )
                if latest_status is not None:
                    row.status = latest_status
        if updated:
            # 调用方可能继续使用同一 ORM 对象发布本地取消信号。
            row.status = WorkbenchRunStatus.CANCELLING.value
        return updated

    def get_message_page(
        self,
        *,
        conversation_id: UUID,
        before: UUID | None = None,
        page_size: int | None = None,
    ) -> WorkbenchMessagePageResp:
        """按业务消息游标读取一页对话。"""
        # Handler 已通过 CursorPaginatorReq 完成类型和范围校验；这里仅保留
        # Service 的默认值，避免再次把字符串强制转换成整数。
        limit = self._bounded_page_size(page_size)
        try:
            # 1.按稳定的 created_at + id 游标定位消息窗口。
            query = self.db.session.query(Message).filter(
                Message.conversation_id == conversation_id,
                Message.is_deleted.is_(False),
            )
            if before is not None:
                cursor = (
                    self.db.session.query(Message)
                    .filter(
                        Message.id == before,
                        Message.conversation_id == conversation_id,
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
            # 2.倒序取数、正序输出，并返回下一页游标。
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
        conversation_id: UUID,
        active_run: WorkbenchRun | None,
    ) -> WorkbenchStateResp:
        """水合首屏消息、历史事件和最新状态快照。"""
        try:
            # 1.一次查询取得首屏消息及其所属来源的历史 Run。
            message_page, persisted_runs = self._get_state_message_page_and_runs(
                conversation_id=conversation_id,
                invoke_from=invoke_from,
            )
            latest_run = persisted_runs[-1] if persisted_runs else None
            # 2.组装轻量 State、历史事件和当前 Run 元数据。
            values = self._build_state_values(
                active_run=active_run,
                latest_run=latest_run,
                message_page=message_page,
            )
            historical_events = self._collect_historical_events(
                persisted_runs=persisted_runs,
                active_run=active_run,
            )
            values["workbench_events"] = [
                event.model_dump(mode="json")
                for event in sorted(historical_events, key=lambda item: item.seq)
            ]
            run_id, status, last_event_sequence = self._state_run_metadata(
                active_run=active_run,
                latest_run=latest_run,
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

    # -------------------- Internal state assembly --------------------

    def _get_state_message_page_and_runs(
        self,
        *,
        conversation_id: UUID,
        invoke_from: InvokeFrom,
        page_size: int = 20,
    ) -> tuple[WorkbenchMessagePageResp, list[AgentRun]]:
        """用一次查询取得首屏消息及其关联 Run。"""
        limit = self._bounded_page_size(page_size)
        # 1.消息和当前入口对应的 Run 在一次查询中关联。
        descending_rows = (
            self.db.session.query(Message, AgentRun)
            .outerjoin(
                AgentRun,
                and_(
                    AgentRun.message_id == Message.id,
                    AgentRun.conversation_id == conversation_id,
                    AgentRun.invoke_from == invoke_from.value,
                ),
            )
            .filter(
                Message.conversation_id == conversation_id,
                Message.is_deleted.is_(False),
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit + 1)
            .all()
        )
        # 2.将查询结果投影为正序消息页和 Run 列表。
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
    def _build_state_values(
        *,
        active_run: WorkbenchRun | None,
        latest_run: AgentRun | None,
        message_page: WorkbenchMessagePageResp,
    ) -> dict[str, Any]:
        """将最新快照、消息页投影为工作台 values。"""
        source_snapshot: Mapping[str, Any] = {}
        if active_run is not None and isinstance(active_run.state_snapshot, Mapping):
            source_snapshot = active_run.state_snapshot
        elif latest_run is not None and isinstance(latest_run.state_snapshot, Mapping):
            source_snapshot = latest_run.state_snapshot
        values = {
            key: source_snapshot[key]
            for key in _STATE_SNAPSHOT_KEYS
            if key in source_snapshot
        }
        values["messages"] = [
            message.model_dump(mode="json") for message in message_page.messages
        ]
        values["message_page"] = message_page.page.model_dump(mode="json")
        return values

    def _collect_historical_events(
        self,
        *,
        persisted_runs: list[AgentRun],
        active_run: WorkbenchRun | None,
    ) -> list[WorkbenchEvent]:
        """合并数据库和本地缓冲事件，并按事件 ID 去重。"""
        # 1.读取数据库中已经落库的工具和生命周期事件。
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

        # 2.补充当前 Worker 的实时事件，并过滤已落库的重复项。
        if active_run is None:
            return historical_events
        known_event_ids = {event.event_id for event in historical_events}
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
        return historical_events

    @staticmethod
    def _state_run_metadata(
        *,
        active_run: WorkbenchRun | None,
        latest_run: AgentRun | None,
    ) -> tuple[UUID | None, str, int]:
        """计算首屏 Run 标识、状态和事件游标。"""
        if active_run is not None:
            return (
                active_run.run_id,
                active_run.status.value,
                active_run.last_event_sequence,
            )
        if latest_run is None:
            return None, "idle", 0
        return latest_run.id, str(latest_run.status), int(
            latest_run.last_event_sequence or 0
        )

    # -------------------- Internal projection helpers --------------------

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

    @staticmethod
    def _bounded_page_size(page_size: int | None = None) -> int:
        """统一工作台历史消息页大小，避免不同入口出现边界差异。"""
        return min(max(page_size if page_size is not None else 20, 1), 50)


def _state_snapshot_event(
    run_id: UUID,
    sequence: int,
    snapshot: Mapping[str, Any] | None,
) -> WorkbenchEvent | None:
    """把 Run 最新快照压缩成一条历史水合事件。"""
    if not snapshot:
        return None
    data = {
        key: snapshot[key]
        for key in _STATE_SNAPSHOT_KEYS
        if key in snapshot
    }
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


__all__ = ["AgentWorkbenchRunService"]
