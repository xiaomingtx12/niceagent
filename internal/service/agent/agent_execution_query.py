"""Agent 执行只读视图与事件订阅，不创建执行或提交业务状态。"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC
import time
from typing import Any
from uuid import UUID

from injector import inject
from sqlalchemy import case, func, or_
from sqlalchemy.dialects.postgresql import array
from sqlalchemy.orm import Session, defer, load_only
from internal.entity.conversation_entity import InvokeFrom
from internal.entity import AGENT_EXECUTION_ACTIVE_STATUSES, AGENT_EXECUTION_TERMINAL_STATUSES
from internal.exception import NotFoundException
from internal.model import Account, AgentExecution, AgentExecutionEvent, ConversationTurn
from internal.schema.agent.agent_model_schema import (
    AgentProtocolModelSettingsResp,
)
from internal.schema.agent.agent_protocol_schema import (
    PUBLIC_EVENT_METHODS, AgentProtocolEvent, AgentProtocolEventStreamReq, AgentProtocolStateResp,
    AgentProtocolMessagePageInfoResp, AgentProtocolRunMetadataResp, AgentProtocolRunSummaryResp,
    AgentProtocolRunEventsResp, AgentProtocolHistoryMessageResp,
    AgentProtocolRecoverySnapshotResp,
)
from internal.service.agent_protocol.event_projector import AgentProtocolEventProjector
from internal.core.agent_v2.entity.stream_event import (
    DeltaKind, EventChannel, MessageEventKind, TOOL_ARGUMENT_CONTENT_TYPES,
)
from internal.service.agent_protocol.state_projector import select_public_snapshot
from internal.service.agent_protocol.recovery_snapshot import compact_execution_events
from internal.service.conversation.conversation_service import ConversationService
from pkg.sqlalchemy import SQLAlchemy
from .agent_model_service import AgentModelService
from .agent_execution_service import AgentExecutionService


# 等待表单只结束当前事件流，Run 本身仍允许在提交答案后恢复。
_SSE_FINISHED_STATUSES = frozenset((*AGENT_EXECUTION_TERMINAL_STATUSES, "waiting_input"))


@inject
@dataclass
class AgentExecutionQuery:
    """调用方先校验会话权限；历史读数据库，活动 SSE 优先读取实时尾部。"""

    db: SQLAlchemy
    run_service: AgentExecutionService
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
            self.db.session.query(AgentExecution.id)
            .filter(
                AgentExecution.id == run_id,
                AgentExecution.app_id == app_id,
                AgentExecution.conversation_id == conversation_id,
                AgentExecution.invoke_from == invoke_from.value,
            )
            .one_or_none()
        )
        if owned is None:
            raise NotFoundException("Agent Run 不存在或不属于当前会话")
        self.db.session.close()
        # 2.返回延迟执行的生成器，首批事件会在 SSE 响应开始迭代后读取。
        return self._stream_events(
            conversation_id=conversation_id,
            stream_request=stream_request, run_id=run_id,
        )

    def get_state(
        self, *, conversation_id: UUID, invoke_from: InvokeFrom,
    ) -> AgentProtocolStateResp:
        """读取已授权会话在指定入口下的状态。"""
        # 1.问答、Run 摘要和最新展示状态来自同一个只读数据库快照。
        rows, persisted_runs, message_page = self._read_state(conversation_id, invoke_from)
        latest_run = persisted_runs[-1] if persisted_runs else None
        runs_by_message = {run.turn_id: run for run in persisted_runs}
        messages = [
            message
            for turn in rows
            for message in self._turn_messages(turn, runs_by_message.get(turn.id))
        ]

        # 2.仅用活动 Run 的已提交快照补齐本轮消息，其他回合和已结束运行不受覆盖。
        source_snapshot: Mapping[str, Any] = {}
        if latest_run is not None:
            source_snapshot = latest_run.public_snapshot
        values = select_public_snapshot(source_snapshot)
        values["messages"] = messages
        if latest_run is not None and latest_run.status in AGENT_EXECUTION_ACTIVE_STATUSES:
            current_ids = {
                str(latest_run.input_message_id),
                f"{latest_run.turn_id}:human",
                f"{latest_run.turn_id}:assistant",
            }
            by_id = {message["id"]: message for message in values["messages"]}
            for message in source_snapshot.get("messages", []):
                if isinstance(message, dict) and message.get("id") in current_ids:
                    by_id[message["id"]] = message
            values["messages"] = list(by_id.values())
        values["message_page"] = message_page.model_dump(mode="json")

        # 3.状态接口只返回摘要；失败原因以 Run 自身记录为准，旧记录回落到业务消息。
        message_errors = {message.id: message.error for message in rows}
        values["runs"] = [
            self._run_summary(run, fallback_error=message_errors.get(run.turn_id)).model_dump(mode="json")
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
        tasks = []
        if status == "waiting_input":
            for item in values.get("pending_inputs", []):
                interrupt_id = item["interrupt_id"]
                interrupt = {
                    "id": interrupt_id,
                    "value": item["payload"],
                    "ns": item.get("namespace", []),
                }
                tasks.append({
                    "id": interrupt_id,
                    "name": "ask_user",
                    "interrupts": [interrupt],
                })
        return AgentProtocolStateResp(
            values=values,
            next=["agent"] if status in AGENT_EXECUTION_ACTIVE_STATUSES else [],
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
            self.db.session.query(AgentExecution.app_id, AgentExecution.conversation_id)
            .filter(AgentExecution.id == run_id)
            .one_or_none()
        )
        if row is None:
            raise NotFoundException("Agent Run 不存在")
        self.conversation_service.get_conversation(
            conversation_id=row.conversation_id, app_id=row.app_id, account=account,
        )
        # 追加式事件按主键序号分页，多取一条判断下一页，不执行总数查询。
        rows = (
            self.db.session.query(AgentExecutionEvent.payload)
            .filter(
                AgentExecutionEvent.execution_id == run_id,
                AgentExecutionEvent.payload["method"].astext.in_(PUBLIC_EVENT_METHODS),
            )
            .order_by(AgentExecutionEvent.sequence.asc())
            .offset((current_page - 1) * page_size)
            .limit(page_size + 1)
            .all()
        )
        events = [
            event for row in rows[:page_size]
            if (event := AgentProtocolEventProjector.parse_stored_event(row.payload)) is not None
        ]
        return AgentProtocolRunEventsResp(
            run_id=run_id, events=events,
            next_page=current_page + 1 if len(rows) > page_size else None,
        )

    def get_recovery_snapshot(self, *, run_id: UUID, account: Account) -> AgentProtocolRecoverySnapshotResp:
        """历史详情沿用会话所有权校验，返回整段正文和工具边界而非逐 Token 分页。"""
        row = (
            self.db.session.query(AgentExecution.app_id, AgentExecution.conversation_id)
            .filter(AgentExecution.id == run_id)
            .one_or_none()
        )
        if row is None:
            raise NotFoundException("Agent Run 不存在")
        self.conversation_service.get_conversation(
            conversation_id=row.conversation_id, app_id=row.app_id, account=account,
        )
        return self._build_recovery_snapshot(run_id)

    def get_model_settings(
        self, *, account_id: UUID, app_id: UUID, conversation_id: UUID, created_by: UUID,
        invoke_from: InvokeFrom,
    ) -> AgentProtocolModelSettingsResp:
        """独立读取当前可用模型配置，恢复最近一次覆盖时不加载消息和运行事件。"""
        # 1.只投影最近任务的模型字段，避免读取大快照、观测数据及触发状态恢复。
        invocation = AgentExecution.execution_options
        row = (
            self.db.session.query(
                invocation["model_selection"].label("model_selection"),
                invocation["model_selection_explicit"].label("model_selection_explicit"),
                invocation["reasoning_effort"].label("reasoning_effort"),
            )
            .filter(
                AgentExecution.conversation_id == conversation_id,
                AgentExecution.invoke_from == invoke_from.value,
            )
            .order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc())
            .first()
        )
        # 2.没有历史覆盖时传空字典，恢复规则仍然由模型服务决定。
        return self.model_service.restore_model_settings(
            account_id=account_id, app_id=app_id, created_by=created_by,
            invoke_from=invoke_from, saved=dict(row._mapping) if row is not None else {},
        )

    def _read_state(
        self, conversation_id: UUID, invoke_from: InvokeFrom,
    ) -> tuple[list[ConversationTurn], list[AgentExecution], AgentProtocolMessagePageInfoResp]:
        """短只读事务内固定同一快照，防止终态已更新而问答仍来自提交前。"""
        # 1.授权已完成，先释放请求的读取连接；新 Session 不复用授权阶段的 ORM 缓存。
        self.db.session.close()
        engine = self.db.engine.execution_options(
            isolation_level="REPEATABLE READ", postgresql_readonly=True,
        )
        with Session(bind=engine, autoflush=False) as session:
            # 2.初始化只取第一页，沿用会话历史的 created_at/id 排序与多取一条规则。
            descending = (
                session.query(ConversationTurn)
                .options(defer(ConversationTurn.token_usage, raiseload=True))
                .filter(ConversationTurn.conversation_id == conversation_id, ConversationTurn.is_deleted.is_(False))
                .order_by(ConversationTurn.created_at.desc(), ConversationTurn.id.desc())
                .limit(21)
                .all()
            )
            page_rows = descending[:20]
            page = AgentProtocolMessagePageInfoResp(
                has_more=len(descending) > 20,
                next_before=page_rows[-1].id if len(descending) > 20 else None,
            )
            # 3.历史 Run 只加载摘要；最新 Run 即便不在消息页内也不能遗漏。
            latest_run_id = (
                session.query(AgentExecution.id)
                .filter(AgentExecution.conversation_id == conversation_id, AgentExecution.invoke_from == invoke_from.value)
                .order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc())
                .limit(1)
                .scalar_subquery()
            )
            runs = (
                session.query(AgentExecution)
                .options(load_only(
                    AgentExecution.id, AgentExecution.turn_id, AgentExecution.input_message_id,
                    AgentExecution.status, AgentExecution.last_event_sequence, AgentExecution.created_at,
                    AgentExecution.finished_at, AgentExecution.error_message, raiseload=True,
                ))
                .filter(
                    AgentExecution.conversation_id == conversation_id,
                    AgentExecution.invoke_from == invoke_from.value,
                    or_(AgentExecution.turn_id.in_([turn.id for turn in page_rows]), AgentExecution.id == latest_run_id),
                )
                .order_by(AgentExecution.created_at.asc(), AgentExecution.id.asc())
                .all()
            )
            # 4.只为最新 Run 读取展示大字段，仍处于同一个数据库快照。
            if runs:
                session.refresh(runs[-1], attribute_names=["display_state", "pending_inputs", "input_history"])
            return list(reversed(page_rows)), runs, page

    def _stream_events(
        self, *, conversation_id: UUID, stream_request: AgentProtocolEventStreamReq,
        run_id: UUID,
    ) -> Iterator[dict[str, Any] | None]:
        """历史前缀接实时尾部；两者共用序号，慢订阅者回数据库补齐缺口。"""
        # 1.工作台先固定展示水位；旧客户端仍按传入游标续传。
        cursor = stream_request.since
        if stream_request.recovery == "snapshot":
            try:
                snapshot = self._build_recovery_snapshot(run_id)
            finally:
                self.db.session.close()
            yield snapshot.model_dump(mode="json")
            cursor = snapshot.sequence
            if snapshot.run.status in _SSE_FINISHED_STATUSES:
                return
        heartbeat_at = time.monotonic() + self._SSE_HEARTBEAT_SECONDS
        while True:
            # 2.先记录通知版本，内存发布、写库和片段退出都能唤醒本次等待。
            event_hub = self.run_service.tracker.event_hub(conversation_id, run_id)
            # 活动 Run 等待自己的事件中心；没有活动中心时按 SSE 心跳重新查库。
            version = event_hub.change_token() if event_hub else None
            events = event_hub.read_after(cursor, self._SSE_BATCH_SIZE) if event_hub else None
            status = None
            if events is None:
                try:
                    # 3.实时缓冲覆盖不到的历史从库补；先读终态，再扫描同次提交的尾部。
                    status = self.run_service.get_run_status(run_id)
                    rows = (
                        self.db.session.query(AgentExecutionEvent.sequence, AgentExecutionEvent.payload)
                        .filter(
                            # 归属已在响应开始前验证，执行身份与归属创建后不可变。
                            AgentExecutionEvent.execution_id == run_id,
                            AgentExecutionEvent.sequence > cursor,
                        )
                        .order_by(AgentExecutionEvent.sequence.asc())
                        .limit(self._SSE_BATCH_SIZE)
                        .all()
                    )
                finally:
                    # 等待下一次发布期间不占数据库连接。
                    self.db.session.close()
                events = []
                for row in rows:
                    event = AgentProtocolEventProjector.parse_stored_event(row.payload)
                    if event is not None:
                        events.append(event)
                scanned_count = len(rows)
                if rows:
                    cursor = rows[-1].sequence
            else:
                scanned_count = len(events)
                if events:
                    cursor = events[-1].seq

            # 4.只发送匹配订阅的公开事件；游标包含已扫描的隐藏事件。
            for event in events:
                if not stream_request.matches(event):
                    continue
                yield event.model_dump(mode="json")

            # 5.最终状态只在持久化收尾后发布，看到它即可结束订阅。
            stream_finished = any(
                event.method == EventChannel.LIFECYCLE
                and not event.params.namespace
                and isinstance(event.params.data, Mapping)
                and event.params.data.get("status") in _SSE_FINISHED_STATUSES
                for event in events
            )
            if time.monotonic() >= heartbeat_at:
                # 6.心跳仅维持 SSE 连接，不进入业务事件流。
                yield None
                heartbeat_at = time.monotonic() + self._SSE_HEARTBEAT_SECONDS
            # 7.满批继续读；终态退出，其余等待实时发布、提交或心跳。
            if stream_finished:
                return
            if scanned_count == self._SSE_BATCH_SIZE:
                continue
            if not events and status in _SSE_FINISHED_STATUSES:
                return
            # 数据库查询期间可能已产生尚未提交的实时尾部，先切回内存再等待。
            if event_hub is not None and status is not None and scanned_count:
                continue
            wait_timeout = max(0.0, heartbeat_at - time.monotonic())
            if event_hub:
                event_hub.wait_for_change(version, timeout=wait_timeout)
            else:
                time.sleep(wait_timeout)

    def _build_recovery_snapshot(self, run_id: UUID) -> AgentProtocolRecoverySnapshotResp:
        """先固定事务已提交的水位，再读取不可变事件前缀；构造期间新增事件由后续 SSE 交付。"""
        # 1.一起读取执行和问答记录，固定此次恢复的状态与已提交水位。
        run, turn = (
            self.db.session.query(AgentExecution, ConversationTurn)
            .options(defer(AgentExecution.execution_options))
            .join(ConversationTurn, ConversationTurn.id == AgentExecution.turn_id)
            .filter(AgentExecution.id == run_id)
            .populate_existing()
            .one()
        )
        sequence = int(run.last_event_sequence or 0)
        summary = self._run_summary(run, fallback_error=turn.error)
        # 2.会话正文从业务表恢复，执行细节只使用公开字段。
        values = select_public_snapshot(run.public_snapshot)
        values["messages"] = self._turn_messages(turn, run)
        values["runs"] = [summary.model_dump(mode="json")]
        # 3.只合并固定水位之内的事件；水位之后由实时流继续交付。
        events = compact_execution_events(self._recovery_events(run_id, sequence))
        return AgentProtocolRecoverySnapshotResp(
            run_id=run_id, sequence=sequence, run=summary, values=values, events=events,
        )

    def _recovery_events(self, run_id: UUID, sequence: int) -> Iterator[AgentProtocolEvent]:
        """按当前协议读取历史，在数据库内排除工具参数分片并裁剪重复消息。"""
        payload = AgentExecutionEvent.payload
        data = payload["params"]["data"]
        # 1.工具参数统一使用 block-delta.fields，与实时投影共用内容类型词表。
        argument_delta = (
            (payload["method"].astext == EventChannel.MESSAGES.value)
            & (data["event"].astext == MessageEventKind.BLOCK_DELTA.value)
            & (data["delta"]["type"].astext == DeltaKind.FIELDS.value)
            & data["delta"]["fields"]["type"].astext.in_(TOOL_ARGUMENT_CONTENT_TYPES)
        )
        # 2.在数据库侧裁掉重复聊天历史，避免先传输完整大载荷再由 Python 丢弃。
        public_payload = case(
            (payload["method"].astext == EventChannel.VALUES.value, payload.op("#-")(array(["params", "data", "messages"]))),
            else_=payload,
        )
        # 3.按序分批读取公开前缀，不改变原有事件序号。
        rows = (
            self.db.session.query(public_payload.label("payload"))
            .filter(
                AgentExecutionEvent.execution_id == run_id,
                AgentExecutionEvent.sequence <= sequence,
                ~func.coalesce(argument_delta, False),
            )
            .order_by(AgentExecutionEvent.sequence.asc())
            .yield_per(self._SSE_BATCH_SIZE)
        )
        for row in rows:
            event = AgentProtocolEventProjector.parse_stored_event(row.payload)
            if event is not None:
                yield event

    @staticmethod
    def _turn_messages(turn: ConversationTurn, run: AgentExecution | None = None) -> list[dict[str, Any]]:
        """state 与恢复快照共用问答和用量投影，消息身份与实时事件一致。"""
        run_id = str(run.id) if run is not None else turn.trace_id
        input_message_id = run.input_message_id if run is not None else None
        messages = [AgentProtocolHistoryMessageResp(
            id=input_message_id or f"{turn.id}:human",
            type="human",
            content=turn.query,
            additional_kwargs={
                "business_message_id": str(turn.id),
                "run_id": run_id,
                "image_urls": list(turn.image_urls or []),
                "attachments": list(turn.attachments or []),
            },
        )]
        if turn.answer:
            messages.append(AgentProtocolHistoryMessageResp(
                id=f"{turn.id}:assistant",
                type="ai",
                content=turn.answer,
                additional_kwargs={
                    "run_id": run_id,
                    "metrics": {
                        "latency": float(turn.latency or 0),
                        "total_token_count": int(turn.total_token_count or 0),
                        "input_token_count": int(turn.input_token_count or 0),
                        "output_token_count": int(turn.output_token_count or 0),
                        "cached_token_count": int(turn.cached_token_count or 0),
                        "reasoning_token_count": int(turn.reasoning_token_count or 0),
                    },
                },
            ))
        return [message.model_dump(mode="json") for message in messages]

    @staticmethod
    def _run_summary(run: AgentExecution, *, fallback_error: str | None = None) -> AgentProtocolRunSummaryResp:
        """运行摘要只读取轻量列，错误优先使用 Run 自身记录。"""
        return AgentProtocolRunSummaryResp(
            run_id=run.id, message_id=run.turn_id, input_message_id=run.input_message_id,
            status=run.status, last_event_sequence=int(run.last_event_sequence or 0),
            created_at=run.created_at.replace(tzinfo=UTC), started_at=run.created_at.replace(tzinfo=UTC),
            finished_at=run.finished_at.replace(tzinfo=UTC) if run.finished_at else None,
            error=(run.error_message or fallback_error or "Agent 运行失败") if run.status == "failed" else None,
        )
