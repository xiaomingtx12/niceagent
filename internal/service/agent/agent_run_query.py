"""Agent Run 只读视图与事件订阅，不创建运行或提交业务状态。"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC
import time
from typing import Any
from uuid import UUID

from injector import inject
from sqlalchemy import or_
from internal.entity.conversation_entity import InvokeFrom
from internal.entity import AGENT_RUN_ACTIVE_STATUSES, AGENT_RUN_TERMINAL_STATUSES
from internal.exception import NotFoundException
from internal.model import Account, AgentRun, AgentRunEvent
from internal.schema.agent.agent_model_schema import (
    AgentProtocolModelSettingsResp,
)
from internal.schema.agent.agent_protocol_schema import (
    PUBLIC_EVENT_METHODS, AgentProtocolEventStreamReq, AgentProtocolStateResp,
    AgentProtocolMessagePageInfoResp, AgentProtocolRunMetadataResp, AgentProtocolRunSummaryResp,
    AgentProtocolRunEventsResp,
)
from internal.service.agent_protocol.event_projector import AgentProtocolEventProjector
from internal.service.agent_protocol.message_projector import project_conversation_messages, merge_running_messages
from internal.service.agent_protocol.state_projector import project_snapshot_values, project_pending_tasks
from internal.service.conversation.conversation_service import ConversationService
from pkg.sqlalchemy import SQLAlchemy
from .agent_model_service import AgentModelService
from .agent_run_events import event_signal
from .agent_run_service import AgentRunService


@inject
@dataclass
class AgentRunQuery:
    """调用方先校验会话权限；快照和 SSE 都只读取已提交数据。"""

    db: SQLAlchemy
    run_service: AgentRunService
    model_service: AgentModelService
    conversation_service: ConversationService

    _SSE_HEARTBEAT_SECONDS = 15
    _SSE_BATCH_SIZE = 256

    def stream_events(
        self, *, app_id: UUID, conversation_id: UUID, invoke_from: InvokeFrom,
        stream_request: AgentProtocolEventStreamReq,
    ) -> Iterator[dict[str, Any] | None]:
        """创建事件流；归属校验在请求阶段完成，连接由 Flask 上下文回收。"""
        # 1.会话权限已在 Handler 中校验；Run 归属必须在这里确认，异常要在响应头发出前抛出。
        run_id = stream_request.run_id
        owned = (
            self.db.session.query(AgentRun.id)
            .filter(
                AgentRun.id == run_id,
                AgentRun.app_id == app_id,
                AgentRun.conversation_id == conversation_id,
                AgentRun.invoke_from == invoke_from.value,
            )
            .one_or_none()
        )
        if owned is None:
            raise NotFoundException("Agent Run 不存在或不属于当前会话")
        # 2.返回延迟执行的生成器，首批事件会在 SSE 响应开始迭代后读取。
        return self._stream_events(
            conversation_id=conversation_id, invoke_from=invoke_from,
            stream_request=stream_request, run_id=run_id,
        )

    def get_state(
        self, *, conversation_id: UUID, invoke_from: InvokeFrom,
    ) -> AgentProtocolStateResp:
        """读取已授权会话在指定入口下的状态。"""
        # 1.重连和轮询都从这里读真实状态，只读不写；崩溃残留的运行中记录由下一次写操作收敛。
        rows, has_more, next_before = self.conversation_service.get_message_page(
            conversation_id=conversation_id,
            page_size=20,
        )
        message_page = AgentProtocolMessagePageInfoResp(
            has_more=has_more,
            next_before=next_before,
        )
        # 状态不受消息分页截断：一条查询同时取回消息页关联 Run 和当前入口最新 Run。
        message_ids = [message.id for message in rows]
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
        persisted_runs = (
            self.db.session.query(AgentRun)
            .filter(
                AgentRun.conversation_id == conversation_id,
                AgentRun.invoke_from == invoke_from.value,
                or_(AgentRun.message_id.in_(message_ids), AgentRun.id == latest_run_id),
            )
            .order_by(AgentRun.created_at.asc(), AgentRun.id.asc())
            .all()
        )
        messages = project_conversation_messages(
            rows, runs_by_message={run.message_id: run for run in persisted_runs}
        )
        latest_run = persisted_runs[-1] if persisted_runs else None

        # 2.只读取已提交的快照，状态查询与 SSE 订阅使用同一数据源。
        source_snapshot: Mapping[str, Any] = {}
        if latest_run is not None:
            source_snapshot = latest_run.public_snapshot
        values = project_snapshot_values(source_snapshot)
        values["messages"] = [
            message.model_dump(mode="json") for message in messages
        ]
        values["messages"] = merge_running_messages(
            values["messages"], latest_run, source_snapshot,
        )
        values["message_page"] = message_page.model_dump(mode="json")

        # 3.状态接口只返回摘要；失败原因以 Run 自身记录为准，旧记录回落到业务消息。
        message_errors = {message.id: message.error for message in rows}
        values["runs"] = [
            AgentProtocolRunSummaryResp(
                run_id=run.id, message_id=run.message_id,
                input_message_id=run.input_message_id, status=run.status,
                last_event_sequence=int(run.last_event_sequence or 0),
                created_at=run.created_at.replace(tzinfo=UTC),
                started_at=run.started_at.replace(tzinfo=UTC) if run.started_at else None,
                finished_at=run.finished_at.replace(tzinfo=UTC) if run.finished_at else None,
                error=(run.error_message or message_errors.get(run.message_id) or "Agent 运行失败")
                if run.status == "failed" else None,
            ).model_dump(mode="json")
            for run in persisted_runs
        ]

        # 4.当前运行优先，找不到运行记录时返回空闲状态。
        if latest_run is None:
            run_id, status, last_event_sequence = None, "idle", 0
        else:
            run_id = latest_run.id
            status = str(latest_run.status)
            last_event_sequence = int(latest_run.last_event_sequence or 0)
        # 5.等待输入时才生成待办表单，避免普通状态响应构造无效任务。
        tasks = project_pending_tasks(status, values)
        return AgentProtocolStateResp(
            values=values,
            next=["agent"] if status in AGENT_RUN_ACTIVE_STATUSES else [],
            tasks=tasks,
            metadata=AgentProtocolRunMetadataResp(
                run_id=run_id,
                status=status,
                last_event_sequence=last_event_sequence,
            ),
        )

    def list_run_events(
        self, *, run_id: UUID, account: Account, current_page: int, page_size: int,
    ) -> AgentProtocolRunEventsResp:
        """校验 Run 所属会话的访问权限，再分页读取已保存的执行事件。"""
        row = (
            self.db.session.query(AgentRun.app_id, AgentRun.conversation_id)
            .filter(AgentRun.id == run_id)
            .one_or_none()
        )
        if row is None:
            raise NotFoundException("Agent Run 不存在")
        self.conversation_service.get_conversation(
            conversation_id=row.conversation_id, app_id=row.app_id, account=account,
        )
        # 追加式事件按主键序号分页，多取一条判断下一页，不执行总数查询。
        rows = (
            self.db.session.query(AgentRunEvent.payload)
            .filter(
                AgentRunEvent.run_id == run_id,
                AgentRunEvent.payload["method"].astext.in_(PUBLIC_EVENT_METHODS),
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
        return AgentProtocolRunEventsResp(
            run_id=run_id, events=events,
            next_page=current_page + 1 if len(rows) > page_size else None,
        )

    def get_model_settings(
        self, *, account_id: UUID, app_id: UUID, conversation_id: UUID, created_by: UUID,
        invoke_from: InvokeFrom,
    ) -> AgentProtocolModelSettingsResp:
        """独立读取当前可用模型配置，恢复最近一次覆盖时不加载消息和运行事件。"""
        # 1.只投影最近任务的模型字段，避免读取大快照、观测数据及触发状态恢复。
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
        # 2.没有历史覆盖时传空字典，恢复规则仍然由模型服务决定。
        return self.model_service.restore_model_settings(
            account_id=account_id, app_id=app_id, created_by=created_by,
            invoke_from=invoke_from, saved=dict(row._mapping) if row is not None else {},
        )

    def _stream_events(
        self, *, conversation_id: UUID, invoke_from: InvokeFrom, stream_request: AgentProtocolEventStreamReq,
        run_id: UUID,
    ) -> Iterator[dict[str, Any] | None]:
        """读取已提交事件并产生 SSE 心跳哨兵；通知只用于唤醒，数据库才是重连数据源。"""
        # 1.从客户端游标开始续传；游标只根据已扫描的事件批次推进。
        cursor = stream_request.since
        heartbeat_at = time.monotonic() + self._SSE_HEARTBEAT_SECONDS
        while True:
            # 2.先记录通知版本，再查询数据库，避免查询期间的提交被漏掉。
            version = event_signal.snapshot()
            try:
                # 3.先观察状态再扫描事件，看到终态时本次扫描一定覆盖终态提交。
                status = self.run_service.get_run_status(run_id)
                # 4.隐藏事件同样占用一批配额，游标按扫描到的行推进而不是按已发送事件推进。
                rows = (
                    self.db.session.query(AgentRunEvent.sequence, AgentRunEvent.payload)
                    .join(AgentRun, AgentRun.id == AgentRunEvent.run_id)
                    .filter(
                        AgentRun.conversation_id == conversation_id,
                        AgentRun.invoke_from == invoke_from.value,
                        AgentRunEvent.sequence > cursor,
                        AgentRun.id == run_id,
                    )
                    .order_by(AgentRunEvent.sequence.asc())
                    .limit(self._SSE_BATCH_SIZE)
                    .all()
                )
            finally:
                # 5.事件和状态查询结束后立即释放连接，等待通知期间不占用 Session。
                self.db.session.close()

            # 6.投影失败和过滤掉的事件都不发送，但都随扫描游标一起越过，不会被重复读取。
            events = [
                event for row in rows
                if (event := AgentProtocolEventProjector.to_public_event(row.payload)) is not None
            ]
            for event in events:
                if stream_request.matches(event):
                    yield event.model_dump(mode="json")

            # 7.只有本批扫描到的行才推进游标；终态事件由本事务提交，看到它即可结束。
            terminal = any(
                event.method == "lifecycle"
                and not event.params.namespace
                and isinstance(event.params.data, Mapping)
                and event.params.data.get("status") in AGENT_RUN_TERMINAL_STATUSES
                for event in events
            )
            if rows:
                cursor = rows[-1].sequence

            if time.monotonic() >= heartbeat_at:
                # 8.心跳仅维持 SSE 连接；None 由响应适配器编码为注释行，不进入业务事件流。
                yield None
                heartbeat_at = time.monotonic() + self._SSE_HEARTBEAT_SECONDS
            if len(rows) < self._SSE_BATCH_SIZE:
                if terminal:
                    return
                if not events and status in AGENT_RUN_TERMINAL_STATUSES:
                    return
                # 9.当前批次已读完才等待通知；仍有后续批次时立即继续读取。
                # 单进程内提交必然触发唤醒，空闲时睡到下一次心跳，不做固定节奏空轮询。
                event_signal.wait(version, timeout=max(0.0, heartbeat_at - time.monotonic()))
