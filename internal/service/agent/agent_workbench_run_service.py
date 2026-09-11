"""工作台 Run 摘要和协议事件的持久化服务。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from uuid import UUID

from injector import inject
from sqlalchemy import func, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import defer, load_only

from internal.entity import (
    WORKBENCH_ACTIVE_STATUSES,
    WORKBENCH_TERMINAL_STATUSES,
    WorkbenchRun,
    WorkbenchRunStatus,
)
from internal.service.agent_protocol.event_projector import (
    AgentProtocolEventProjector,
)
from internal.service.agent_protocol.state_projector import project_snapshot_values
from internal.entity.conversation_entity import InvokeFrom
from internal.exception import FailException, ValidateErrorException, NotFoundException
from internal.model import AgentRun, AgentRunEvent, Message, Conversation
from internal.entity.agent_run_interaction import AgentRunInteraction
from internal.entity.agent_invocation import (
    AgentInvocation,
    AgentInvocationContext,
)
from internal.core.agent_v2.entity.user_input import build_confirmed_input_context
from internal.schema.agent.agent_workbench_schema import (
    WorkbenchEvent,
    WorkbenchRunMetadataResp,
)
from internal.schema.agent.agent_workbench_read_schema import WorkbenchRunEventsResp
from .agent_workbench_event_queue import EventBatch
from pkg.sqlalchemy import SQLAlchemy

_PERSISTED_EVENT_METHODS = ("tools", "lifecycle", "custom")


@inject
@dataclass
class AgentWorkbenchRunService:
    """负责 Run 记录、历史事件和消息终态的数据库读写。"""

    db: SQLAlchemy
    lease_seconds: ClassVar[int] = 120

    def get_confirmed_input_context(self, req: AgentInvocation) -> str:
        """只加载同一授权会话入口的近期答案，不从客户端接收或写入长期记忆。"""
        rows = (
            self.db.session.query(AgentRun.state_snapshot["input_history"])
            .filter(AgentRun.conversation_id == req.conversation_id, AgentRun.app_id == req.app_id,
                    AgentRun.invoke_from == req.invoke_from.value,
                    AgentRun.state_snapshot["input_history"].astext.isnot(None))
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(8).all()
        )
        return build_confirmed_input_context([history for (history,) in rows if isinstance(history, list)])

    def get_context_run(self, context: AgentInvocationContext, run_id: UUID) -> AgentRun:
        """确认 Run 同时属于当前应用、会话和入口；此校验不能触发失联恢复写入。"""
        row = self.get_run(run_id, recover=False)
        if (
            row is None
            or row.app_id != context.app_id
            or row.conversation_id != context.conversation_id
            or row.invoke_from != context.invoke_from.value
        ):
            raise NotFoundException("Agent Run 不存在或不属于当前会话")
        return row

    def lock_conversation(self, context: AgentInvocationContext) -> None:
        """锁定当前会话，保证同一会话的启动检查和创建串行执行。"""
        conversation = (
            self.db.session.query(Conversation.id)
            .filter(
                Conversation.id == context.conversation_id,
                Conversation.app_id == context.app_id,
                Conversation.is_deleted.is_(False),
            )
            .with_for_update()
            .one_or_none()
        )
        if conversation is None:
            raise NotFoundException("会话不存在或已删除")

    def queue_resumed_run(
        self, row: AgentRun, run: WorkbenchRun, snapshot: Mapping[str, Any],
    ) -> None:
        """调用方持有会话和 Run 锁，在同一事务中交接答案、状态和新执行租约。"""
        if row.status != WorkbenchRunStatus.WAITING_INPUT.value:
            raise ValidateErrorException("当前任务没有等待填写的表单")
        row.state_snapshot = dict(snapshot)
        row.status = WorkbenchRunStatus.QUEUED.value
        row.executor_id = run.executor_id
        row.heartbeat_at = datetime.now(UTC).replace(tzinfo=None)
        row.finished_at = None

    def reopen_pending_run(
        self, row: AgentRun, interaction: AgentRunInteraction,
        snapshot: Mapping[str, Any], cancellation_sequence: int,
    ) -> int:
        """调用方持锁且已验证恢复规则；表单、事件和消息状态由此原子更新。"""
        projector = AgentProtocolEventProjector(row.id, start_sequence=int(row.last_event_sequence or 0))
        events = projector.generated_state_transition(project_snapshot_values(snapshot), {
            "event": "interrupted", "status": "waiting_input", "reason": "input_reopened",
            "message_id": str(row.message_id), "input_message_id": row.input_message_id,
        })
        for raw in events:
            event = WorkbenchEvent.model_validate(raw)
            self.db.session.add(AgentRunEvent(
                run_id=row.id, sequence=event.seq, payload=event.model_dump(mode="json"),
            ))
        sequence = projector.last_event_sequence
        row.state_snapshot = interaction.after_reopen(snapshot, cancellation_sequence, sequence)
        row.status = WorkbenchRunStatus.WAITING_INPUT.value
        row.finished_at = None
        row.executor_id = None
        row.last_event_sequence = sequence
        message = self.db.session.get(Message, row.message_id)
        if message is not None:
            message.status = "normal"
        return sequence

    def add_run(self, run: WorkbenchRun) -> AgentRun:
        """在调用方事务中创建平台级 ``AgentRun`` 数据库记录。"""
        # 1.校验运行上下文，建立平台 Run 摘要。
        if run.spec.app_id is None:
            raise ValidateErrorException("工作台 Run 缺少 app_id")
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
            executor_id=run.executor_id,
            heartbeat_at=datetime.now(UTC).replace(tzinfo=None),
            state_snapshot={"_interaction": {"invocation": run.spec.model_dump(mode="json"), "submissions": []}},
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
                .populate_existing()
                .with_for_update()
                .filter(AgentRun.id == run.run_id)
                .one_or_none()
            )
            if row is None or row.executor_id != run.executor_id:
                raise FailException("Agent 运行已失效，请重新发送消息")
            if row.status in WORKBENCH_TERMINAL_STATUSES:
                raise FailException("Agent 运行已结束，请重新发送消息")
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
        events: list[WorkbenchEvent] | None = None,
        state_snapshot: Mapping[str, Any] | None = None,
    ) -> bool:
        """在一个事务中收敛 Run、业务消息和平台观测快照。"""
        # 1.锁定 Run，执行权失效或已写入终态时忽略迟到的结果。
        with self.db.auto_commit():
            row = (
                self.db.session.query(AgentRun)
                .populate_existing()
                .with_for_update()
                .filter(AgentRun.id == run.run_id)
                .one_or_none()
            )
            if row is None or row.executor_id != run.executor_id:
                return False
            if row.status in WORKBENCH_TERMINAL_STATUSES:
                return False

            # 2.仅第一次成功收敛的调用保存运行结果和结束时间。
            row.status = status.value
            row.error_message = error_message
            if row.finished_at is None and status is not WorkbenchRunStatus.WAITING_INPUT:
                row.finished_at = datetime.now(UTC).replace(tzinfo=None)
            if state_snapshot is not None:
                row.state_snapshot = {**(row.state_snapshot or {}), **state_snapshot}
            if observability is not None:
                row.observability = dict(observability)

            # 3.业务消息和 Run 在同一事务内更新。
            if message_updates:
                message = self.db.session.get(Message, run.spec.message_id)
                if message is not None:
                    for field, value in message_updates.items():
                        if run.resume is not None and field in {
                            "latency", "model_call_count", "tool_call_count", "input_token_count",
                            "output_token_count", "cached_token_count", "reasoning_token_count", "total_token_count",
                        }:
                            value += getattr(message, field, 0) or 0
                        setattr(message, field, value)
            if events:
                row.last_event_sequence = max(
                    int(row.last_event_sequence or 0),
                    max(event.seq for event in events),
                )
                statement = pg_insert(AgentRunEvent).values(
                    [
                        {
                            "run_id": run.run_id,
                            "sequence": event.seq,
                            "payload": event.model_dump(mode="json"),
                        }
                        for event in events
                    ]
                )
                self.db.session.execute(
                    statement.on_conflict_do_nothing(
                        index_elements=["run_id", "sequence"]
                    )
                )
        return True

    def record_events(
        self,
        run: WorkbenchRun,
        events: list[WorkbenchEvent],
    ) -> None:
        """一次事务更新 Run 快照并追加一批需要历史恢复的事件。"""
        # 1.所有公开事件使用同一持久化游标，重连不能跳过消息增量。
        if not events:
            return

        latest_sequence = max(event.seq for event in events)
        latest_snapshot = None
        snapshot_sequence = -1
        persisted_payloads = {}
        for event in events:
            is_root_snapshot = event.method == "values" and not event.params.namespace
            if is_root_snapshot and isinstance(event.params.data, dict):
                if event.seq > snapshot_sequence:
                    latest_snapshot = event.params.data
                    snapshot_sequence = event.seq
            persisted_payloads[event.seq] = {
                "run_id": run.run_id,
                "sequence": event.seq,
                "payload": event.model_dump(mode="json"),
            }

        # 2.在一个事务中更新 Run 快照并幂等追加事件。
        with self.db.auto_commit():
            columns = [AgentRun.executor_id, AgentRun.status, AgentRun.last_event_sequence]
            if latest_snapshot is not None:
                columns.append(AgentRun.state_snapshot)
            row = (
                self.db.session.query(AgentRun)
                .options(load_only(*columns))
                .populate_existing()
                .with_for_update()
                .filter(AgentRun.id == run.run_id)
                .one_or_none()
            )
            if row is None:
                raise FailException("Agent 运行已失效，不能继续发布事件")
            owns_execution = row.executor_id == run.executor_id
            has_finished = row.status in WORKBENCH_TERMINAL_STATUSES
            if not owns_execution or has_finished:
                raise FailException("Agent 运行已失效，不能继续发布事件")
            previous_sequence = int(row.last_event_sequence or 0)
            row.last_event_sequence = max(previous_sequence, latest_sequence)
            if latest_snapshot is not None and snapshot_sequence >= previous_sequence:
                row.state_snapshot = {**(row.state_snapshot or {}), **latest_snapshot}
            if persisted_payloads:
                statement = pg_insert(AgentRunEvent).values(
                    list(persisted_payloads.values())
                )
                self.db.session.execute(
                    statement.on_conflict_do_nothing(
                        index_elements=["run_id", "sequence"]
                    )
                )

    def renew_lease(self, run: WorkbenchRun) -> bool:
        """只允许当前执行实例续租；已超时或已结束的 Run 不能被旧线程复活。"""
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.db.auto_commit():
            updated = (
                self.db.session.query(AgentRun)
                .filter(
                    AgentRun.id == run.run_id,
                    AgentRun.executor_id == run.executor_id,
                    AgentRun.status.in_(WORKBENCH_ACTIVE_STATUSES),
                    AgentRun.heartbeat_at >= now - timedelta(seconds=self.lease_seconds),
                )
                .update({AgentRun.heartbeat_at: now}, synchronize_session=False)
            )
        return bool(updated)

    def recover_stale_runs(
        self, *, conversation_id: UUID | None = None, run_id: UUID | None = None
    ) -> None:
        """按访问范围收敛失联 Run；终态、消息和 SSE 事件在同一事务提交。"""
        # 未指定范围时用于后台 sweeper 扫描全局失联 Run；带范围时仅收敛对应记录。
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.db.auto_commit():
            query = self.db.session.query(AgentRun).filter(
                AgentRun.status.in_(WORKBENCH_ACTIVE_STATUSES),
                func.coalesce(AgentRun.heartbeat_at, AgentRun.started_at, AgentRun.created_at)
                < now - timedelta(seconds=self.lease_seconds),
            )
            if conversation_id is not None:
                query = query.filter(AgentRun.conversation_id == conversation_id)
            if run_id is not None:
                query = query.filter(AgentRun.id == run_id)
            for row in query.populate_existing().with_for_update().all():
                cancelled = row.status == WorkbenchRunStatus.CANCELLING.value
                row.status = (
                    WorkbenchRunStatus.CANCELLED.value
                    if cancelled
                    else WorkbenchRunStatus.FAILED.value
                )
                row.error_message = "Agent 执行进程已失联，请重新发送消息。"
                row.finished_at = now
                # 撤销执行权，使迟到的事件、结果和续租无法覆盖恢复后的状态。
                row.executor_id = None
                row.last_event_sequence = int(row.last_event_sequence or 0) + 1
                event = WorkbenchEvent(
                    type="event",
                    event_id=f"{row.id}:{row.last_event_sequence}",
                    seq=row.last_event_sequence,
                    method="lifecycle",
                    params={"data": {
                        "event": "interrupted" if cancelled else "failed",
                        "status": row.status,
                        "error": row.error_message,
                        "message_id": str(row.message_id),
                        "input_message_id": row.input_message_id,
                    }},
                )
                self.db.session.add(AgentRunEvent(
                    run_id=row.id, sequence=event.seq, payload=event.model_dump(mode="json")
                ))
                message = self.db.session.get(Message, row.message_id)
                if message is not None:
                    message.status = "stop" if cancelled else "error"
                    message.error = row.error_message

    def get_run(self, run_id: UUID, *, recover: bool = False) -> AgentRun | None:
        """默认只读；调用方确认归属后才能显式恢复，并负责关闭 ORM Session。"""
        if recover:
            self.recover_stale_runs(run_id=run_id)
        return self.db.session.get(AgentRun, run_id, populate_existing=True)

    def get_run_by_input_message(
        self,
        *,
        conversation_id: UUID,
        input_message_id: str | None,
    ) -> AgentRun | None:
        """按输入消息读取幂等 Run；没有幂等标识时直接返回。"""
        if input_message_id is None:
            return None
        return (
            self.db.session.query(AgentRun)
            .filter(
                AgentRun.conversation_id == conversation_id,
                AgentRun.input_message_id == input_message_id,
            )
            .one_or_none()
        )

    def get_run_status(self, run_id: UUID) -> str | None:
        """取消监控只读取状态列，避免重复传输和解析运行快照。"""
        return self.db.session.query(AgentRun.status).filter(AgentRun.id == run_id).scalar()

    def get_initial_event_sequence(self, run: AgentRun) -> int:
        """重复提交也从本 Run 开头重放，不能把终态游标当作执行起点。"""
        first_sequence = (
            self.db.session.query(func.min(AgentRunEvent.sequence))
            .filter(AgentRunEvent.run_id == run.id)
            .scalar()
        )
        return (
            int(first_sequence) - 1
            if first_sequence is not None
            else int(run.last_event_sequence or 0)
        )

    def list_state_runs(
        self,
        *,
        conversation_id: UUID,
        invoke_from: InvokeFrom,
        message_ids: list[UUID],
    ) -> list[AgentRun]:
        """一条查询读取消息页关联 Run 及当前入口最新 Run，状态不受消息分页截断。"""
        latest_run_id = (
            self.db.session.query(AgentRun.id)
            .filter(
                AgentRun.conversation_id == conversation_id,
                AgentRun.invoke_from == invoke_from.value,
            )
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        return (
            self.db.session.query(AgentRun)
            .options(defer(AgentRun.observability))
            .filter(
                AgentRun.conversation_id == conversation_id,
                AgentRun.invoke_from == invoke_from.value,
                or_(AgentRun.message_id.in_(message_ids), AgentRun.id == latest_run_id),
            )
            .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
            .all()
        )

    def get_latest_model_overrides(
        self, *, conversation_id: UUID, invoke_from: InvokeFrom,
    ) -> dict[str, Any]:
        """只投影最近任务的模型字段，避免读取大快照、观测数据及触发状态恢复。"""
        invocation = AgentRun.state_snapshot["_interaction"]["invocation"]
        row = (
            self.db.session.query(
                invocation["model_selection"].label("model_selection"),
                invocation["model_selection_explicit"].label("model_selection_explicit"),
                invocation["reasoning_effort"].label("reasoning_effort"),
            )
            .filter(
                AgentRun.conversation_id == conversation_id,
                AgentRun.invoke_from == invoke_from.value,
            )
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .first()
        )
        return dict(row._mapping) if row is not None else {}

    def get_latest_run(
        self,
        *,
        conversation_id: UUID,
        invoke_from: InvokeFrom,
        recover: bool = True,
    ) -> AgentRun | None:
        """读取会话指定来源的最新 Run，并释放只读 Session。"""
        if recover:
            self.recover_stale_runs(conversation_id=conversation_id)
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

    def get_last_event_sequence(
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

    def get_run_metadata(self, *, conversation_id: UUID, invoke_from: InvokeFrom) -> WorkbenchRunMetadataResp:
        """停止操作只需要运行标识与状态，不读取消息和 JSON 快照。"""
        row = (
            self.db.session.query(AgentRun.id, AgentRun.status, AgentRun.last_event_sequence)
            .filter(AgentRun.conversation_id == conversation_id, AgentRun.invoke_from == invoke_from.value)
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .first()
        )
        return WorkbenchRunMetadataResp(
            run_id=row.id, status=row.status, last_event_sequence=row.last_event_sequence,
        ) if row else WorkbenchRunMetadataResp()

    def get_run_event_page(self, *, run_id: UUID, current_page: int, page_size: int) -> WorkbenchRunEventsResp:
        """追加式事件按主键序号分页，多取一条判断下一页，不执行总数查询。"""
        rows = (
            self.db.session.query(AgentRunEvent.payload)
            .filter(
                AgentRunEvent.run_id == run_id,
                AgentRunEvent.payload["method"].astext.in_(_PERSISTED_EVENT_METHODS),
            )
            .order_by(AgentRunEvent.sequence.asc())
            .offset((current_page - 1) * page_size)
            .limit(page_size + 1)
            .all()
        )
        events = [
            event for row in rows[:page_size]
            if (event := AgentProtocolEventProjector.to_public_event(row.payload)) is not None
        ]
        return WorkbenchRunEventsResp(
            run_id=run_id, events=events,
            next_page=current_page + 1 if len(rows) > page_size else None,
        )

    def list_events_by_run(
        self,
        run_ids: list[UUID],
    ) -> dict[UUID, list[WorkbenchEvent]]:
        """只读取历史投影需要的列，避免为每个事件构造 ORM 实体。"""
        if not run_ids:
            return {}
        rows = (
            self.db.session.query(AgentRunEvent.run_id, AgentRunEvent.payload)
            .filter(
                AgentRunEvent.run_id.in_(run_ids),
                AgentRunEvent.payload["method"].astext.in_(_PERSISTED_EVENT_METHODS),
            )
            .order_by(
                AgentRunEvent.run_id.asc(),
                AgentRunEvent.sequence.asc(),
            )
            .all()
        )
        events: dict[UUID, list[WorkbenchEvent]] = {run_id: [] for run_id in run_ids}
        for row in rows:
            visible = AgentProtocolEventProjector.to_public_event(row.payload)
            if visible is not None:
                events.setdefault(row.run_id, []).append(visible)
        return events

    def list_context_events_by_run(self, run_ids: list[UUID]) -> dict[UUID, list[WorkbenchEvent]]:
        """每个延后加载的 Run 最多读取 64 条上下文事件，避免整段历史进入首屏。"""
        if not run_ids:
            return {}
        ranked = self.db.session.query(
            AgentRunEvent.run_id, AgentRunEvent.payload, AgentRunEvent.sequence,
            func.row_number().over(
                partition_by=AgentRunEvent.run_id, order_by=AgentRunEvent.sequence.desc(),
            ).label("position"),
        ).filter(
            AgentRunEvent.run_id.in_(run_ids),
            AgentRunEvent.payload["method"].astext == "custom",
        ).subquery()
        rows = self.db.session.query(ranked.c.run_id, ranked.c.payload).filter(
            ranked.c.position <= 64,
        ).order_by(ranked.c.run_id, ranked.c.sequence).all()
        events: dict[UUID, list[WorkbenchEvent]] = {}
        for row in rows:
            event = AgentProtocolEventProjector.to_public_event(row.payload)
            if event is not None:
                events.setdefault(row.run_id, []).append(event)
        return events

    def get_reconnect_events(
        self,
        *,
        conversation_id: UUID,
        invoke_from: InvokeFrom,
        run_id: UUID | None = None,
        since: int,
        limit: int = 256,
    ) -> EventBatch:
        """按落库序号推进扫描游标，避免隐藏事件占满一批后阻断续传。"""
        query = (
            self.db.session.query(AgentRunEvent.sequence, AgentRunEvent.payload)
            .join(AgentRun, AgentRun.id == AgentRunEvent.run_id)
            .filter(
                AgentRun.conversation_id == conversation_id,
                AgentRun.invoke_from == invoke_from.value,
                AgentRunEvent.sequence > since,
            )
        )
        if run_id is not None:
            query = query.filter(AgentRun.id == run_id)
        rows = (
            query
            .order_by(AgentRunEvent.sequence.asc())
            .limit(limit)
            .all()
        )
        events = []
        for row in rows:
            event = AgentProtocolEventProjector.to_public_event(row.payload)
            if event is not None:
                events.append(event)
        return EventBatch(
            events=events,
            next_sequence=rows[-1].sequence if rows else since,
            has_more=len(rows) == limit,
        )

    def request_cancel(self, row: AgentRun) -> bool:
        """运行中的任务先请求停止；等待表单的任务直接取消并保留重新填写信息。"""
        with self.db.auto_commit():
            # 1.重新读取并锁定目标状态，避免取消请求覆盖已经结束的任务。
            current = (
                self.db.session.query(AgentRun)
                .populate_existing()
                .with_for_update()
                .filter(
                    AgentRun.id == row.id,
                    AgentRun.status.in_(
                        (*WORKBENCH_ACTIVE_STATUSES, WorkbenchRunStatus.WAITING_INPUT.value)
                    ),
                )
                .one_or_none()
            )
            if current is None:
                latest_status = (
                    self.db.session.query(AgentRun.status)
                    .filter(AgentRun.id == row.id)
                    .scalar()
                )
                if latest_status is not None:
                    row.status = latest_status
                return False

            # 2.有执行实例时只写 cancelling，实际资源释放和终态由执行线程负责。
            if current.status != WorkbenchRunStatus.WAITING_INPUT.value:
                current.status = WorkbenchRunStatus.CANCELLING.value
            else:
                self._cancel_pending_input(current)
                row.status = WorkbenchRunStatus.CANCELLED.value
                return True

        # 调用方在提交后用最新状态发送本地取消信号。
        row.status = WorkbenchRunStatus.CANCELLING.value
        return True

    def _cancel_pending_input(self, current: AgentRun) -> None:
        """在调用方锁定的事务中保留表单恢复信息，并同步消息与终态事件。"""
        # 1.等待表单时没有运行线程，直接取消；保存原表单用于重新填写。
        current.status = WorkbenchRunStatus.CANCELLED.value
        current.finished_at = datetime.now(UTC).replace(tzinfo=None)
        cancellation_sequence = int(current.last_event_sequence or 0) + 2
        snapshot = AgentRunInteraction(
            WorkbenchRunStatus.WAITING_INPUT.value, current.state_snapshot
        ).after_cancel(
            current.id, cancellation_sequence,
        )
        current.executor_id = None
        current.state_snapshot = snapshot

        # 2.表单清空、消息停止和结束事件必须一次提交，重连才能恢复相同状态。
        projector = AgentProtocolEventProjector(
            current.id, start_sequence=int(current.last_event_sequence or 0),
        )
        public_state = project_snapshot_values(snapshot)
        events = projector.generated_state_transition(public_state, {
            "event": "interrupted", "status": "cancelled", "reason": "cancel_requested",
            "message_id": str(current.message_id), "input_message_id": current.input_message_id,
        })
        for raw in events:
            event = WorkbenchEvent.model_validate(raw)
            self.db.session.add(AgentRunEvent(
                run_id=current.id,
                sequence=event.seq,
                payload=event.model_dump(mode="json"),
            ))
        current.last_event_sequence = projector.last_event_sequence
        message = self.db.session.get(Message, current.message_id)
        if message is not None:
            message.status = "stop"


__all__ = ["AgentWorkbenchRunService"]
