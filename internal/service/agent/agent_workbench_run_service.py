"""工作台 Run 摘要和协议事件的持久化服务。"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from injector import inject
from sqlalchemy import func, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import load_only

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
from internal.model import AgentRun, AgentRunSubmission, AgentRunEvent, Message, Conversation
from internal.entity.agent_run_interaction import AgentRunInteraction
from internal.entity.agent_invocation import (
    AgentInvocation,
)
from internal.core.agent_v2 import (
    build_confirmed_input_context,
)
from internal.schema.agent.agent_workbench_schema import (
    WorkbenchEvent,
    WorkbenchRunMetadataResp,
)
from internal.schema.agent.agent_workbench_read_schema import WorkbenchRunEventsResp
from pkg.sqlalchemy import SQLAlchemy

# 历史回放必须包含思考、阶段正文和计划状态，不能只恢复工具及生命周期。
_PERSISTED_EVENT_METHODS = ("messages", "values", "tools", "lifecycle", "custom", "input.requested")


@dataclass(frozen=True)
class EventBatch:
    """内部扫描结果；游标独立于过滤后的事件，不作为 HTTP 响应校验或序列化。"""

    events: list[WorkbenchEvent]
    next_sequence: int
    has_more: bool


@inject
@dataclass
class AgentWorkbenchRunService:
    """负责 Run 记录、历史事件和消息终态的数据库读写。"""

    db: SQLAlchemy

    def get_confirmed_input_context(self, req: AgentInvocation) -> str:
        """只加载同一授权会话入口的近期答案，不从客户端接收或写入长期记忆。"""
        rows = (
            self.db.session.query(AgentRun.interaction_state["input_history"])
            .filter(AgentRun.conversation_id == req.conversation_id, AgentRun.app_id == req.app_id,
                    AgentRun.invoke_from == req.invoke_from.value,
                    AgentRun.interaction_state["input_history"].astext.isnot(None))
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(8).all()
        )
        return build_confirmed_input_context([history for (history,) in rows if isinstance(history, list)])

    def get_conversation_run(
        self, *, app_id: UUID, conversation_id: UUID, invoke_from: InvokeFrom, run_id: UUID,
    ) -> AgentRun:
        """确认 Run 同时属于当前应用、会话和入口；此校验不能触发失联恢复写入。"""
        row = self.get_run(run_id)
        if (
            row is None
            or row.app_id != app_id
            or row.conversation_id != conversation_id
            or row.invoke_from != invoke_from.value
        ):
            raise NotFoundException("Agent Run 不存在或不属于当前会话")
        return row

    def lock_conversation(
        self, *, app_id: UUID, conversation_id: UUID,
    ) -> None:
        """锁定当前会话，保证同一会话的启动检查和创建串行执行。"""
        conversation = (
            self.db.session.query(Conversation.id)
            .filter(
                Conversation.id == conversation_id,
                Conversation.app_id == app_id,
                Conversation.is_deleted.is_(False),
            )
            .with_for_update()
            .one_or_none()
        )
        if conversation is None:
            raise NotFoundException("会话不存在或已删除")

    def lock_latest_run(
        self, *, app_id: UUID, conversation_id: UUID, invoke_from: InvokeFrom,
    ) -> AgentRun | None:
        """调用方先锁会话；提交答案只锁定当前应用和入口的最新 Run，不提前释放事务。"""
        return (
            self.db.session.query(AgentRun)
            .populate_existing()
            .filter(
                AgentRun.conversation_id == conversation_id,
                AgentRun.app_id == app_id,
                AgentRun.invoke_from == invoke_from.value,
            )
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .with_for_update()
            .first()
        )

    def get_latest_conversation_run(
        self, conversation_id: UUID, *, for_update: bool = False,
    ) -> AgentRun | None:
        """跨入口读取会话最后一次运行；重新填写不能跳过其他入口后来创建的 Run。

        锁定时调用方须先锁会话，本方法保留调用方 Session 和事务。
        """
        query = self.db.session.query(AgentRun)
        if for_update:
            query = query.populate_existing().with_for_update()
        return (
            query.filter(AgentRun.conversation_id == conversation_id)
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .first()
        )

    def get_blocking_run(self, conversation_id: UUID) -> AgentRun | None:
        """启动前检查整个会话的占用，等待表单也阻止新运行；调用方持有会话锁。"""
        return (
            self.db.session.query(AgentRun)
            .filter(
                AgentRun.conversation_id == conversation_id,
                AgentRun.status.in_(WORKBENCH_OCCUPYING_STATUSES),
            )
            .first()
        )

    def get_submitted_sequence(self, row: AgentRun, answers: Mapping[str, Any]) -> int | None:
        """调用方已锁定 Run；先查询历史批次，使恢复后的重试仍返回首次游标。"""
        submission = self.db.session.get(
            AgentRunSubmission, (row.id, AgentRunInteraction.input_batch_id(answers)),
        )
        if submission is None:
            return None
        if submission.answers != answers:
            raise ValidateErrorException("该表单已提交，不能重复修改")
        return submission.accepted_sequence

    def queue_resumed_run(
        self, row: AgentRun, run: WorkbenchRun, answers: Mapping[str, Any],
    ) -> None:
        """调用方持有会话和 Run 锁；答案、交互投影和新执行租约原子保存。"""
        if row.status != WorkbenchRunStatus.WAITING_INPUT.value:
            raise ValidateErrorException("当前任务没有等待填写的表单")
        if run.resume is None:
            raise ValueError("恢复执行缺少已校验答案")
        row.interaction_state = AgentRunInteraction(row.status, row.interaction_state).after_submission(run.resume)
        self.db.session.add(AgentRunSubmission(
            run_id=row.id, input_batch_id=AgentRunInteraction.input_batch_id(answers),
            answers=deepcopy(dict(answers)), accepted_sequence=int(row.last_event_sequence or 0),
        ))
        row.status = WorkbenchRunStatus.RUNNING.value
        row.finished_at = None
        self._acquire_execution(row, run)

    def reopen_pending_run(
        self, row: AgentRun, cancellation_sequence: int,
    ) -> int:
        """调用方持锁且已验证恢复规则；表单、事件和消息状态由此原子更新。"""
        interaction = AgentRunInteraction(row.status, row.interaction_state)
        row.interaction_state = interaction.restored_state()
        events = self._state_transition_events(row, {
            "event": "interrupted", "status": "waiting_input", "reason": "input_reopened",
            "message_id": str(row.message_id), "input_message_id": row.input_message_id,
        })
        self._append_events(row, events)
        sequence = row.last_event_sequence
        row.interaction_state = interaction.after_reopen(row.interaction_state, cancellation_sequence, sequence)
        row.status = WorkbenchRunStatus.WAITING_INPUT.value
        row.finished_at = None
        row.executor_id = None
        # 表单重新待填说明上次取消已被接管，清空标记让冲突提示回到“请先完成当前表单”。
        row.cancel_requested_at = None
        message = self.db.session.get(Message, row.message_id)
        if message is not None:
            message.status = "normal"
        return sequence

    def add_run(self, run: WorkbenchRun) -> AgentRun:
        """在调用方事务中创建平台级 ``AgentRun`` 数据库记录。"""
        # 1.使用已校验的运行上下文建立平台 Run 摘要。
        row = AgentRun(
            id=run.run_id,
            app_id=run.spec.app_id,
            conversation_id=run.conversation_id,
            invoke_from=run.spec.invoke_from.value,
            message_id=run.spec.message_id,
            input_message_id=run.spec.input_message_id,
            status=WorkbenchRunStatus.RUNNING.value,
            last_event_sequence=run.initial_event_sequence,
            created_at=datetime.now(UTC).replace(tzinfo=None),
            invocation_snapshot=run.spec.model_dump(mode="json"),
            interaction_state={}, state_snapshot={},
        )
        # 2.交给调用方事务 flush，保证后续事件可以按 Run 主键写入。
        self.db.session.add(row)
        self._acquire_execution(row, run)
        self.db.session.flush()
        return row

    def mark_running(self, run: WorkbenchRun) -> None:
        """当前执行实例进入运行态；已提交的取消请求不能被启动覆盖。"""
        # 1.在事务内锁定当前 Run，避免并发执行路径覆盖已落库终态。
        with self.db.auto_commit():
            row = self._lock_execution(run)
            if row is None:
                raise FailException("Agent 运行已失效，请重新发送消息")
            # 2.取消已请求时不再宣告运行中，让执行线程按取消结果立即收敛。
            if row.cancel_requested_at is not None:
                return
            row.status = WorkbenchRunStatus.RUNNING.value
            if row.started_at is None:
                row.started_at = datetime.now(UTC).replace(tzinfo=None)

    def finalize_run(
        self,
        run: WorkbenchRun,
        status: WorkbenchRunStatus,
        *,
        message_updates: Mapping[str, Any] | None = None,
        usage: Mapping[str, Any] | None = None,
        events: list[WorkbenchEvent] | None = None,
        state_snapshot: Mapping[str, Any] | None = None,
    ) -> bool:
        """在 Run 锁内一次结算片段用量、消息与事件，重复或过期执行不能再次写入。"""
        if status.value not in (*WORKBENCH_TERMINAL_STATUSES, WorkbenchRunStatus.WAITING_INPUT.value):
            raise ValueError("执行片段只能结算为等待输入或终态")
        # 1.锁定 Run，执行权失效或已写入终态时忽略迟到的结果。
        with self.db.auto_commit():
            row = self._lock_execution(run, with_snapshot=state_snapshot is not None)
            if row is None:
                return False
            if usage and usage.get("trace_id") != str(row.id):
                raise ValueError("不能结算其他 Run 的用量")

            # 2.仅第一次成功收敛的调用保存运行结果和结束时间。
            now = datetime.now(UTC).replace(tzinfo=None)
            row.status = status.value
            row.finished_at = None if status is WorkbenchRunStatus.WAITING_INPUT else now
            # 失败提示同时落到 Run，运行列表不必回查消息即可解释失败原因。
            row.error_message = str(message_updates.get("error") or "") if message_updates else ""
            if state_snapshot is not None:
                row.apply_snapshot(state_snapshot)
            # 与累计用量同事务撤销执行权，暂停后重试也不能重复结算。
            row.executor_id = None

            # 3.业务消息和 Run 在同一事务内更新。
            updates = dict(message_updates or {})
            message = self.db.session.get(Message, row.message_id, populate_existing=True)
            if message is None:
                raise FailException("Agent 运行对应的消息不存在")
            for field, value in updates.items():
                if run.resume is not None and field == "latency":
                    value += getattr(message, field, 0) or 0
                setattr(message, field, value)
            message.apply_usage(usage, accumulate=True)
            self._append_events(row, events or [])
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

        latest_snapshot = None
        snapshot_sequence = -1
        for event in events:
            is_root_snapshot = event.method == "values" and not event.params.namespace
            if is_root_snapshot and isinstance(event.params.data, dict):
                if event.seq > snapshot_sequence:
                    latest_snapshot = event.params.data
                    snapshot_sequence = event.seq

        # 2.在一个事务中更新 Run 快照并幂等追加事件。
        with self.db.auto_commit():
            row = self._lock_execution(run, with_snapshot=latest_snapshot is not None)
            if row is None:
                raise FailException("Agent 运行已失效，不能继续发布事件")
            previous_sequence = int(row.last_event_sequence or 0)
            if latest_snapshot is not None and snapshot_sequence >= previous_sequence:
                row.apply_snapshot(latest_snapshot)
            self._append_events(row, events)

    def settle_orphan_runs(
        self,
        *,
        alive_run_ids: Collection[UUID],
        conversation_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> None:
        """收敛本进程已不再持有的运行记录；终态、消息和公开事件在同一事务提交。

        ``alive_run_ids`` 是进程内注册表持有的执行，只有它之外的运行中记录才是孤儿：
        进程重启、执行线程异常收尾失败都会让数据库停留在运行中，而不会有线程认领它。
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        with self.db.auto_commit():
            query = self.db.session.query(AgentRun).filter(
                AgentRun.status.in_(WORKBENCH_ACTIVE_STATUSES),
            )
            if alive_run_ids:
                query = query.filter(AgentRun.id.notin_(alive_run_ids))
            if conversation_id is not None:
                query = query.filter(AgentRun.conversation_id == conversation_id)
            if run_id is not None:
                query = query.filter(AgentRun.id == run_id)
            for row in query.populate_existing().with_for_update().all():
                # 已请求取消的执行按取消收敛，否则说明执行已随进程消失。
                cancelled = row.cancel_requested_at is not None
                row.status = (
                    WorkbenchRunStatus.CANCELLED.value
                    if cancelled
                    else WorkbenchRunStatus.FAILED.value
                )
                public_error = "Agent 执行已中断，请重新发送消息。"
                row.finished_at = now
                row.error_message = "" if cancelled else public_error
                # 撤销执行权，使迟到的事件和结果无法覆盖收敛后的状态。
                row.executor_id = None
                sequence = int(row.last_event_sequence or 0) + 1
                event = WorkbenchEvent(
                    type="event",
                    event_id=f"{row.id}:{sequence}",
                    seq=sequence,
                    method="lifecycle",
                    params={"data": {
                        "event": "interrupted" if cancelled else "failed",
                        "status": row.status,
                        "error": public_error,
                        "message_id": str(row.message_id),
                        "input_message_id": row.input_message_id,
                    }},
                )
                self._append_events(row, [event])
                message = self.db.session.get(Message, row.message_id)
                if message is not None:
                    message.status = "stop" if cancelled else "error"
                    message.error = public_error

    def get_run(self, run_id: UUID) -> AgentRun | None:
        """只读取最新数据库状态；失联恢复和 Session 生命周期由调用方负责。"""
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
        """只读取状态列，避免重复传输和解析运行快照。"""
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
        invocation = AgentRun.invocation_snapshot
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
    ) -> AgentRun | None:
        """只读指定入口的最新 Run，保留调用方 Session 和事务。"""
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

    def get_last_event_sequence(
        self,
        *,
        conversation_id: UUID,
        invoke_from: InvokeFrom,
    ) -> int:
        """在调用方事务中读取已持久化的最大序号，不关闭 Session 或释放会话锁。"""
        return int(
            self.db.session.query(func.max(AgentRun.last_event_sequence))
            .filter(
                AgentRun.conversation_id == conversation_id,
                AgentRun.invoke_from == invoke_from.value,
            )
            .scalar()
            or 0
        )

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

    def get_reconnect_events(
        self,
        *,
        conversation_id: UUID,
        invoke_from: InvokeFrom,
        run_id: UUID,
        since: int,
        limit: int = 256,
    ) -> EventBatch:
        """按落库序号推进扫描游标，避免隐藏事件占满一批后阻断续传。"""
        rows = (
            self.db.session.query(AgentRunEvent.sequence, AgentRunEvent.payload)
            .join(AgentRun, AgentRun.id == AgentRunEvent.run_id)
            .filter(
                AgentRun.conversation_id == conversation_id,
                AgentRun.invoke_from == invoke_from.value,
                AgentRunEvent.sequence > since,
                AgentRun.id == run_id,
            )
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
        """运行中的任务先记录取消请求；等待表单的任务直接取消并保留重新填写信息。"""
        with self.db.auto_commit():
            # 1.重新读取并锁定目标状态，避免取消请求覆盖已经结束的任务。
            current = (
                self.db.session.query(AgentRun)
                .populate_existing()
                .with_for_update()
                .filter(
                    AgentRun.id == row.id,
                    AgentRun.status.in_(WORKBENCH_OCCUPYING_STATUSES),
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

            # 2.重复点击不刷新取消时刻，执行线程只认首次请求。
            if current.cancel_requested_at is None:
                current.cancel_requested_at = datetime.now(UTC).replace(tzinfo=None)

            # 3.有执行实例时只记录请求，资源释放和终态由执行线程负责。
            if current.status != WorkbenchRunStatus.WAITING_INPUT.value:
                row.cancel_requested_at = current.cancel_requested_at
                return True

            self._cancel_pending_input(current)
            row.status = WorkbenchRunStatus.CANCELLED.value
            row.cancel_requested_at = current.cancel_requested_at
            return True

    def _cancel_pending_input(self, current: AgentRun) -> None:
        """在调用方锁定的事务中保留表单恢复信息，并同步消息与终态事件。"""
        # 1.等待表单时没有运行线程，直接取消；保存原表单用于重新填写。
        current.status = WorkbenchRunStatus.CANCELLED.value
        current.finished_at = datetime.now(UTC).replace(tzinfo=None)
        cancellation_sequence = int(current.last_event_sequence or 0) + 2
        current.interaction_state = AgentRunInteraction(
            WorkbenchRunStatus.WAITING_INPUT.value, current.interaction_state
        ).after_cancel(
            current.id, cancellation_sequence,
        )
        current.executor_id = None

        # 2.表单清空、消息停止和结束事件必须一次提交，重连才能恢复相同状态。
        events = self._state_transition_events(current, {
            "event": "interrupted", "status": "cancelled", "reason": "cancel_requested",
            "message_id": str(current.message_id), "input_message_id": current.input_message_id,
        })
        self._append_events(current, events)
        message = self.db.session.get(Message, current.message_id)
        if message is not None:
            message.status = "stop"

    def _acquire_execution(self, row: AgentRun, run: WorkbenchRun) -> None:
        """调用方持有创建权或 Run 锁；只分配新执行段，不复用已经结算的执行身份。"""
        if row.executor_id is not None:
            raise ValidateErrorException("当前任务仍有未结束的执行")
        row.executor_id = run.executor_id
        # 新执行段从零开始：上一段的取消请求不能阻止本段正常开始。
        row.cancel_requested_at = None

    def _lock_execution(
        self, run: WorkbenchRun, *, with_snapshot: bool = False,
    ) -> AgentRun | None:
        """只锁定并刷新 Run，统一校验执行身份和活动状态。"""
        columns = [AgentRun.executor_id, AgentRun.status, AgentRun.last_event_sequence,
                   AgentRun.message_id, AgentRun.started_at, AgentRun.finished_at]
        if with_snapshot:
            columns.extend((AgentRun.state_snapshot, AgentRun.interaction_state))
        row = self.db.session.get(
            AgentRun, run.run_id, options=[load_only(*columns)],
            populate_existing=True, with_for_update=True,
        )
        if (row is None or row.executor_id != run.executor_id
                or row.status not in WORKBENCH_ACTIVE_STATUSES):
            return None
        return row

    def _append_events(self, row: AgentRun, events: list[WorkbenchEvent]) -> None:
        """调用方已锁 Run；事件追加与游标更新共用事务，重试不重复插入。"""
        if not events:
            return
        payloads = {
            event.seq: {"run_id": row.id, "sequence": event.seq, "payload": event.model_dump(mode="json")}
            for event in events
        }
        statement = pg_insert(AgentRunEvent).values(list(payloads.values()))
        self.db.session.execute(statement.on_conflict_do_nothing(index_elements=["run_id", "sequence"]))
        row.last_event_sequence = max(int(row.last_event_sequence or 0), max(payloads))

    def _state_transition_events(self, row: AgentRun, lifecycle: Mapping[str, Any]) -> list[WorkbenchEvent]:
        """取消和重新填写共享公开快照投影，私有恢复信息不会进入事件载荷。"""
        projector = AgentProtocolEventProjector(row.id, start_sequence=int(row.last_event_sequence or 0))
        return projector.generated_state_transition(project_snapshot_values(row.public_snapshot), dict(lifecycle))


__all__ = ["AgentWorkbenchRunService"]
