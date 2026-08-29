"""Agent Protocol 工作台的应用服务入口。

本模块只负责 Run 生命周期、并发控制、事件投影和传输适配；
Agent 图的构建与执行委托给 ``AgentAssemblyService`` 和 Agent Core。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Iterator
from uuid import UUID, uuid4

from flask import current_app
from injector import inject
from pydantic import ValidationError
from redis.exceptions import RedisError

from internal.core.agent_v2.protocol.event_projector import (
    AgentProtocolEventProjector,
)
from internal.core.agent_v2.protocol.message_parser import (
    message_content_to_text,
)
from internal.entity import (
    WORKBENCH_TERMINAL_STATUSES,
    WorkbenchRun,
    WorkbenchRunInput,
    WorkbenchRunStartReservation,
    WorkbenchRunStatus,
)
from internal.entity.app_entity import AppStatus
from internal.entity.conversation_entity import InvokeFrom, MessageStatus
from internal.exception import (
    AgentInvocationException,
    ForbiddenException,
    NotFoundException,
    WorkbenchReplayCursorExpired,
)
from internal.model import Account, AgentRun, App, Conversation, EndUser, Message
from internal.repository import AgentWorkbenchRedisRepository
from internal.schema.agent_attachment_schema import AgentAttachmentResolveReq
from internal.schema.agent_attachment_schema import dump_agent_attachments
from internal.schema.agent_runtime_schema import AgentInvocation
from internal.schema.agent_workbench_schema import (
    WorkbenchCancelResp,
    WorkbenchCommandErrorResp,
    WorkbenchCommandReq,
    WorkbenchCommandSuccessResp,
    WorkbenchEvent,
    WorkbenchMessagePageResp,
    WorkbenchStateResp,
    WorkbenchSubscriptionReq,
)
from internal.schema.app_config_schema import AgentAppConfigReq
from pkg.sqlalchemy import SQLAlchemy
from .agent_assembly_service import AgentAssemblyService
from .agent_attachment_service import AgentAttachmentService
from .agent_workbench_access_service import AgentWorkbenchAccessService
from .agent_workbench_run_service import AgentWorkbenchRunService
from .app_config_service import AppConfigService
from .assistant_agent_service import AssistantAgentService
from .base_service import BaseService
from .conversation_service import ConversationService
from .web_app_service import WebAppService


def _is_root_terminal_event(event: WorkbenchEvent) -> bool:
    """只有根 namespace 的应用终态才能结束整个 Run 订阅。"""
    if event.method != "lifecycle" or event.params.namespace:
        return False
    data = event.params.data
    if not isinstance(data, Mapping):
        return False
    return any(
        data.get(field_name) in WORKBENCH_TERMINAL_STATUSES
        for field_name in ("status", "event")
    )


@inject
@dataclass
class AgentWorkbenchService(BaseService):
    """协调一次 Agent Protocol Run 的完整业务流程。

    负责权限入口、Run 入队、数据库状态、取消、事件重放和 SSE 输出，
    不负责查询 App 配置细节，也不直接构建 LangGraph 图。
    """

    db: SQLAlchemy
    agent_assembly_service: AgentAssemblyService
    access_service: AgentWorkbenchAccessService
    run_service: AgentWorkbenchRunService
    redis_repository: AgentWorkbenchRedisRepository
    app_config_service: AppConfigService
    attachment_service: AgentAttachmentService
    web_app_service: WebAppService
    assistant_agent_service: AssistantAgentService

    _runs: dict[str, WorkbenchRun] = field(init=False, default_factory=dict)
    _starting_runs: dict[str, WorkbenchRunStartReservation] = field(
        init=False,
        default_factory=dict,
    )
    _condition: threading.Condition = field(
        init=False,
        default_factory=threading.Condition,
    )
    # 1、Redis 可用时不再用数据库轮询发现下一轮任务；该间隔只作为 Redis
    # 不可用时的低频降级检查间隔，避免长连接持续占用数据库连接。
    _db_poll_interval: ClassVar[float] = 5.0
    _cancel_signal_poll_interval: ClassVar[float] = 0.25
    _cancel_status_poll_interval: ClassVar[float] = 1.0
    _sse_heartbeat_interval: ClassVar[float] = 15.0

    # ---------- 对外工作台查询 ----------

    def subscribe(
        self,
        *,
        invoke_from: InvokeFrom,
        access_value: str,
        thread_id: UUID,
        account: Account,
        subscription: WorkbenchSubscriptionReq,
    ) -> Iterator[dict[str, Any] | None]:
        """校验线程归属后创建事件订阅。"""
        self.access_service.authorize_thread(
            invoke_from=invoke_from,
            access_value=access_value,
            thread_id=thread_id,
            account=account,
        )
        return self._subscribe_events(
            invoke_from=invoke_from,
            thread_id=thread_id,
            subscription=subscription,
        )

    def get_state(
        self,
        *,
        invoke_from: InvokeFrom,
        access_value: str,
        thread_id: UUID,
        account: Account,
    ) -> WorkbenchStateResp:
        """校验线程归属后读取工作台状态。"""
        self.access_service.authorize_thread(
            invoke_from=invoke_from,
            access_value=access_value,
            thread_id=thread_id,
            account=account,
        )
        key = str(thread_id)
        with self._condition:
            active_run = self._runs.get(key)
        return self.run_service.get_state(
            invoke_from=invoke_from,
            thread_id=thread_id,
            active_run=active_run,
        )

    def get_message_page(
        self,
        *,
        invoke_from: InvokeFrom,
        access_value: str,
        thread_id: UUID,
        account: Account,
        before: UUID | None = None,
        page_size: int | None = None,
    ) -> WorkbenchMessagePageResp:
        """校验线程归属后读取一页业务消息。"""
        self.access_service.authorize_thread(
            invoke_from=invoke_from,
            access_value=access_value,
            thread_id=thread_id,
            account=account,
        )
        return self.run_service.get_message_page(
            thread_id=thread_id,
            before=before,
            page_size=page_size,
        )

    # ---------- 跨进程 Run 的事件流 ----------

    def _stream_db_run(
        self,
        db_run: AgentRun,
        *,
        since: int,
        subscription: WorkbenchSubscriptionReq,
    ):
        """从 Redis 重放并跟随其他进程执行的运行，故障时回退数据库。"""
        cursor = since
        heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
        redis_repository = self.redis_repository
        terminal_seen = False
        next_db_check_at = time.monotonic() + self._db_poll_interval
        # Redis Stream 可能暂时没有新事件，因此循环不能用“读一次就结束”。
        # 只有收到根 Run 终态或数据库确认终态时才退出。
        while not terminal_seen:
            try:
                replay_batch = redis_repository.read_events(
                    db_run.id,
                    cursor,
                    block_ms=1000,
                )
            except WorkbenchReplayCursorExpired as expired:
                yield self._cursor_reset_event(
                    db_run,
                    requested_sequence=cursor,
                    oldest_event_sequence=expired.oldest_event_sequence,
                ).model_dump(mode="json")
                cursor = max(0, expired.oldest_event_sequence - 1)
                continue
            events = (
                replay_batch.events
                if replay_batch is not None
                else self.run_service.list_events(
                    db_run.id,
                    after_sequence=cursor,
                )
            )
            for event in events:
                cursor = max(cursor, event.seq)
                if _is_root_terminal_event(event):
                    # 根终态已经进入流后，本轮订阅无需再查询数据库确认结束。
                    terminal_seen = True
                if subscription.matches(event):
                    yield event.model_dump(mode="json")

            # Redis 正常时由阻塞读取等待事件；只有 Redis 不可用时才低频
            # 查询数据库状态，避免每个 SSE 连接持续占用数据库连接。
            latest = None
            now = time.monotonic()
            if replay_batch is None or (not events and now >= next_db_check_at):
                latest = self.run_service.get_run(db_run.id)
                self.db.session.close()
                next_db_check_at = now + self._db_poll_interval
                if (
                    not events
                    and latest is not None
                    and int(latest.last_event_sequence or 0) > cursor
                ):
                    events = self.run_service.list_events(
                        db_run.id,
                        after_sequence=cursor,
                    )
                    for event in events:
                        cursor = max(cursor, event.seq)
                        if _is_root_terminal_event(event):
                            terminal_seen = True
                        if subscription.matches(event):
                            yield event.model_dump(mode="json")
            status = latest.status if latest is not None else db_run.status
            if terminal_seen or (status in WORKBENCH_TERMINAL_STATUSES and not events):
                return

            if events:
                heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
            elif time.monotonic() >= heartbeat_at:
                heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
                yield None
            if replay_batch is None:
                time.sleep(self._db_poll_interval)

    # ---------- 协议快照辅助 ----------

    def _cursor_reset_event(
        self,
        db_run: AgentRun | WorkbenchRun,
        *,
        requested_sequence: int,
        oldest_event_sequence: int,
    ) -> WorkbenchEvent:
        """构造客户端可识别的快照重置事件。"""
        run_id = getattr(db_run, "id", getattr(db_run, "run_id", "unknown"))
        event = {
            "type": "event",
            "event_id": f"{run_id}:cursor-reset:{oldest_event_sequence}",
            "seq": max(0, oldest_event_sequence - 1),
            "method": "values",
            "params": {
                "namespace": [],
                "timestamp": None,
                "data": db_run.state_snapshot or {"messages": [], "files": {}},
                "metadata": {
                    "my_dify": {
                        "cursor_reset": True,
                        "requested_sequence": requested_sequence,
                        "oldest_event_sequence": oldest_event_sequence,
                    }
                },
            },
        }
        visible = AgentProtocolEventProjector.to_public_event(event)
        if visible is None:
            raise ValueError("游标重置事件被协议安全策略拒绝")
        return visible

    def _resolve_attachments(
        self,
        run_input: WorkbenchRunInput,
        *,
        owner_id: UUID,
    ):
        """解析并校验本次 Run 引用的附件。"""
        return self.attachment_service.resolve(
            AgentAttachmentResolveReq.from_values(
                run_input.file_ids,
                owner_id=owner_id,
            )
        )

    # ---------- Run 创建和配置水合 ----------

    def _create_debug_run(
        self,
        *,
        app_id: UUID,
        thread_id: UUID,
        account: Account,
        run_input: WorkbenchRunInput,
        run_id: UUID,
    ) -> WorkbenchRun:
        """创建调试入口的 Message 和 WorkbenchRun。"""
        app = (
            self.db.session.query(App)
            .filter(
                App.id == app_id,
                App.account_id == account.id,
            )
            .one_or_none()
        )
        if app is None:
            raise NotFoundException("应用不存在或无权访问")
        conversation = self.get(Conversation, thread_id)
        if (
            conversation is None
            or conversation.app_id != app_id
            or conversation.invoke_from != InvokeFrom.DEBUGGER
        ):
            raise ForbiddenException("工作台线程不存在或不属于当前应用")
        attachments = self._resolve_attachments(run_input, owner_id=account.id)
        message = Message(
            app_id=app_id,
            conversation_id=conversation.id,
            invoke_from=InvokeFrom.DEBUGGER,
            created_by=account.id,
            query=run_input.query,
            image_urls=list(run_input.image_urls),
            attachments=dump_agent_attachments(attachments),
            status=MessageStatus.NORMAL,
        )
        self.db.session.add(message)
        self.db.session.flush()
        request = AgentInvocation(
            account_id=account.id,
            app_id=app_id,
            app_name=app.name,
            app_description=app.description,
            app_config=AgentAppConfigReq(),
            conversation_id=conversation.id,
            message_id=message.id,
            created_by=account.id,
            user_id=account.id,
            end_user_id=None,
            invoke_from=InvokeFrom.DEBUGGER,
            query=run_input.query,
            image_urls=run_input.image_urls,
            attachments=attachments,
            input_message_id=run_input.input_message_id,
        )
        return WorkbenchRun(
            run_id=run_id,
            thread_id=thread_id,
            spec=request,
            include_opening_statement=conversation.is_new,
        )

    def _create_web_app_run(
        self,
        *,
        token: str,
        thread_id: UUID,
        account: Account,
        run_input: WorkbenchRunInput,
        run_id: UUID,
    ) -> WorkbenchRun:
        """创建 WebApp 入口的 Message 和 WorkbenchRun。"""
        app, end_user, conversation = (
            self.web_app_service.get_or_create_owned_web_conversation(
                token=token,
                conversation_id=thread_id,
                account=account,
                conversation_name=run_input.query,
            )
        )
        attachments = self._resolve_attachments(run_input, owner_id=account.id)
        message = Message(
            app_id=app.id,
            conversation_id=conversation.id,
            invoke_from=InvokeFrom.WEB_APP,
            created_by=end_user.id,
            query=run_input.query,
            image_urls=list(run_input.image_urls),
            attachments=dump_agent_attachments(attachments),
            status=MessageStatus.NORMAL,
        )
        self.db.session.add(message)
        self.db.session.flush()
        request = AgentInvocation(
            account_id=app.account_id,
            app_id=app.id,
            app_name=app.name,
            app_description=app.description,
            app_config=AgentAppConfigReq(),
            conversation_id=conversation.id,
            message_id=message.id,
            created_by=end_user.id,
            user_id=end_user.id,
            end_user_id=end_user.id,
            invoke_from=InvokeFrom.WEB_APP,
            query=run_input.query,
            image_urls=run_input.image_urls,
            attachments=attachments,
            input_message_id=run_input.input_message_id,
        )
        return WorkbenchRun(
            run_id=run_id,
            thread_id=thread_id,
            spec=request,
            include_opening_statement=conversation.is_new,
        )

    def _create_assistant_run(
        self,
        *,
        thread_id: UUID,
        account: Account,
        run_input: WorkbenchRunInput,
        run_id: UUID,
    ) -> WorkbenchRun:
        """创建 Creator 辅助 Agent 入口的 WorkbenchRun。"""
        conversation = account.assistant_agent_conversation
        if conversation.id != thread_id:
            raise ForbiddenException("辅助 Agent 线程不存在或无权访问")
        attachments = self._resolve_attachments(run_input, owner_id=account.id)
        message = Message(
            app_id=conversation.app_id,
            conversation_id=conversation.id,
            invoke_from=InvokeFrom.ASSISTANT_AGENT,
            created_by=account.id,
            query=run_input.query,
            image_urls=list(run_input.image_urls),
            attachments=dump_agent_attachments(attachments),
            status=MessageStatus.NORMAL,
        )
        self.db.session.add(message)
        self.db.session.flush()
        llm, tools = self.assistant_agent_service.prepare_runtime(account)
        request = AgentInvocation(
            account_id=account.id,
            app_id=conversation.app_id,
            app_name="应用构建助手",
            app_description="帮助 Creator 创建应用和工作流。",
            app_config=AgentAppConfigReq(),
            conversation_id=conversation.id,
            message_id=message.id,
            created_by=account.id,
            user_id=account.id,
            end_user_id=None,
            invoke_from=InvokeFrom.ASSISTANT_AGENT,
            query=run_input.query,
            image_urls=run_input.image_urls,
            attachments=attachments,
            extra_tools=tools,
            llm=llm,
            input_message_id=run_input.input_message_id,
        )
        return WorkbenchRun(run_id=run_id, thread_id=thread_id, spec=request)

    def _create_service_api_run(
        self,
        *,
        app_id: UUID,
        thread_id: UUID,
        account: Account,
        run_input: WorkbenchRunInput,
        run_id: UUID,
    ) -> WorkbenchRun:
        """创建 Service API 入口的 Message 和 WorkbenchRun。"""
        app = (
            self.db.session.query(App)
            .filter(
                App.id == app_id,
                App.account_id == account.id,
                App.status == AppStatus.PUBLISHED,
            )
            .one_or_none()
        )
        if app is None:
            raise NotFoundException("应用不存在、未发布或无权访问")
        conversation = self.db.session.get(Conversation, thread_id)
        end_user: EndUser | None = None
        if conversation is not None:
            if (
                conversation.app_id != app.id
                or conversation.invoke_from != InvokeFrom.SERVICE_API
            ):
                raise ForbiddenException("服务 API 线程不属于当前应用")
            end_user = self.db.session.get(EndUser, conversation.created_by)
        elif run_input.end_user_id is not None:
            end_user = (
                self.db.session.query(EndUser)
                .filter(
                    EndUser.id == run_input.end_user_id,
                    EndUser.app_id == app.id,
                    EndUser.tenant_id == account.id,
                )
                .one_or_none()
            )
            if end_user is None:
                raise ForbiddenException("终端用户不存在或不属于当前应用")
        else:
            end_user = EndUser(tenant_id=account.id, app_id=app.id)
            self.db.session.add(end_user)
            self.db.session.flush()
        if conversation is None:
            conversation = Conversation(
                id=thread_id,
                app_id=app.id,
                name=run_input.query[:255],
                invoke_from=InvokeFrom.SERVICE_API,
                created_by=end_user.id,
            )
            self.db.session.add(conversation)
            self.db.session.flush()
        elif end_user is None or conversation.created_by != end_user.id:
            raise ForbiddenException("服务 API 线程终端用户无效")
        attachments = self._resolve_attachments(run_input, owner_id=account.id)
        message = Message(
            app_id=app.id,
            conversation_id=conversation.id,
            invoke_from=InvokeFrom.SERVICE_API,
            created_by=end_user.id,
            query=run_input.query,
            image_urls=list(run_input.image_urls),
            attachments=dump_agent_attachments(attachments),
            status=MessageStatus.NORMAL,
        )
        self.db.session.add(message)
        self.db.session.flush()
        request = AgentInvocation(
            account_id=account.id,
            app_id=app.id,
            app_name=app.name,
            app_description=app.description,
            app_config=AgentAppConfigReq(),
            conversation_id=conversation.id,
            message_id=message.id,
            created_by=end_user.id,
            user_id=end_user.id,
            end_user_id=end_user.id,
            invoke_from=InvokeFrom.SERVICE_API,
            query=run_input.query,
            image_urls=run_input.image_urls,
            attachments=attachments,
            input_message_id=run_input.input_message_id,
        )
        return WorkbenchRun(
            run_id=run_id,
            thread_id=thread_id,
            spec=request,
            include_opening_statement=conversation.is_new,
        )

    def hydrate_config(self, run: WorkbenchRun) -> None:
        """在后台加载本次 Run 的草稿或发布配置。"""
        try:
            app = self.db.session.get(App, run.spec.app_id)
            if app is None:
                raise NotFoundException("应用不存在")
            config = (
                self.app_config_service.get_draft_app_config(app)
                if run.spec.invoke_from == InvokeFrom.DEBUGGER
                else self.app_config_service.get_app_config(app)
            )
            try:
                app_config = AgentAppConfigReq.model_validate(config)
            except ValidationError as exc:
                raise AgentInvocationException(f"应用运行配置无效: {exc}") from exc
            run.spec = run.spec.model_copy(
                update={
                    "app_config": app_config,
                    "app_name": app.name,
                    "app_description": app.description,
                    "opening_statement": (
                        app_config.opening_statement
                        if run.include_opening_statement
                        else ""
                    ),
                }
            )
        finally:
            self.db.session.close()

    def start_run(
        self,
        *,
        invoke_from: InvokeFrom,
        access_value: str,
        thread_id: UUID,
        account: Account,
        command: WorkbenchCommandReq,
    ) -> WorkbenchCommandSuccessResp | WorkbenchCommandErrorResp:
        """校验调用入口，并创建一条待执行的工作台 Run。"""
        self.access_service.authorize_run_start(
            invoke_from=invoke_from,
            access_value=access_value,
            thread_id=thread_id,
            account=account,
        )
        run_input = command.to_run_input()
        input_message_id = run_input.input_message_id

        def create_run(run_id: UUID) -> WorkbenchRun:
            if invoke_from is InvokeFrom.DEBUGGER:
                return self._create_debug_run(
                    app_id=UUID(access_value),
                    thread_id=thread_id,
                    account=account,
                    run_input=run_input,
                    run_id=run_id,
                )
            if invoke_from is InvokeFrom.WEB_APP:
                return self._create_web_app_run(
                    token=access_value,
                    thread_id=thread_id,
                    account=account,
                    run_input=run_input,
                    run_id=run_id,
                )
            if invoke_from is InvokeFrom.ASSISTANT_AGENT:
                return self._create_assistant_run(
                    thread_id=thread_id,
                    account=account,
                    run_input=run_input,
                    run_id=run_id,
                )
            if invoke_from is InvokeFrom.SERVICE_API:
                return self._create_service_api_run(
                    app_id=UUID(access_value),
                    thread_id=thread_id,
                    account=account,
                    run_input=run_input,
                    run_id=run_id,
                )
            raise ValueError("不支持的工作台调用来源")

        return self._enqueue_run(
            thread_id=thread_id,
            invoke_from=invoke_from,
            input_message_id=input_message_id,
            command_id=command.command_id,
            create_run=create_run,
        )

    def start_invocation(
        self,
        *,
        invocation: AgentInvocation,
        thread_id: UUID,
        account: Account,
        command_id: int | None = None,
    ) -> WorkbenchCommandSuccessResp | WorkbenchCommandErrorResp:
        """把微信等内部调用接入统一的 Agent Protocol Run 流程。"""
        self.access_service.authorize_thread(
            invoke_from=invocation.invoke_from,
            access_value=str(invocation.app_id),
            thread_id=thread_id,
            account=account,
        )
        return self._enqueue_run(
            thread_id=thread_id,
            invoke_from=invocation.invoke_from,
            input_message_id=invocation.input_message_id,
            command_id=command_id,
            create_run=lambda run_id: WorkbenchRun(
                run_id=run_id,
                thread_id=thread_id,
                spec=invocation,
            ),
        )

    def _enqueue_run(
        self,
        *,
        thread_id: UUID,
        invoke_from: InvokeFrom,
        input_message_id: str | None,
        command_id: int | None,
        create_run: Callable[[UUID], WorkbenchRun],
    ) -> WorkbenchCommandSuccessResp | WorkbenchCommandErrorResp:
        """统一执行 Run 去重、持久化、排队和后台启动。"""
        key = str(thread_id)
        reservation = WorkbenchRunStartReservation(uuid4(), input_message_id)
        previous_run: WorkbenchRun | None = None

        # 1.先检查本进程状态，快速处理重复提交和同线程并发冲突。
        with self._condition:
            active = self._runs.get(key)
            if (
                active is not None
                and input_message_id is not None
                and active.spec.input_message_id == input_message_id
            ):
                return WorkbenchCommandSuccessResp.create(
                    command_id=command_id,
                    run_id=active.run_id,
                    applied_through_sequence=active.initial_event_sequence,
                )
            if active is not None and not active.done.is_set():
                # 取消请求是异步收敛的：数据库状态会先变成 cancelling，
                # 生产线程要到下一个事件边界才会退出。允许下一轮先入队，
                # 但必须等待前一轮真正结束，避免同一 LangGraph thread 并发写检查点。
                if (
                    active.cancel_requested.is_set()
                    or active.status is WorkbenchRunStatus.CANCELLING
                ):
                    previous_run = active
                else:
                    return WorkbenchCommandErrorResp(
                        id=command_id,
                        message="当前任务已有正在执行的 Agent",
                    )
            starting = self._starting_runs.get(key)
            if starting is not None:
                if (
                    input_message_id is not None
                    and starting.input_message_id == input_message_id
                ):
                    return WorkbenchCommandSuccessResp.create(
                        command_id=command_id,
                        run_id=starting.run_id,
                    )
                return WorkbenchCommandErrorResp(
                    id=command_id,
                    message="当前任务已有正在启动的 Agent",
                )
            self._starting_runs[key] = reservation

        conflict_message: str | None = None
        duplicate_run: AgentRun | None = None
        try:
            # 2.在数据库事务中加线程锁，再执行幂等查询和 Run 入库。
            with self.db.auto_commit():
                self.run_service.lock_thread_start(thread_id)
                duplicate_run = (
                    self.run_service.get_run_by_input_message_id(
                        thread_id=thread_id,
                        input_message_id=input_message_id,
                    )
                    if input_message_id is not None
                    else None
                )
                persisted_active = self.run_service.get_latest_active_thread_run(
                    thread_id
                )
                if duplicate_run is not None:
                    applied_through_sequence = int(
                        duplicate_run.last_event_sequence or 0
                    )
                elif (
                    persisted_active is not None
                    and persisted_active.status != WorkbenchRunStatus.CANCELLING.value
                ):
                    conflict_message = "当前任务已有正在执行的 Agent"
                else:
                    run = create_run(reservation.run_id)
                    persisted_sequence = (
                        self.run_service.get_thread_last_event_sequence(
                            thread_id=thread_id,
                            invoke_from=invoke_from,
                            close_session=False,
                        )
                    )
                    local_sequence = (
                        int(previous_run.last_event_sequence or 0)
                        if previous_run is not None
                        else 0
                    )
                    applied_through_sequence = max(
                        persisted_sequence,
                        local_sequence,
                    )
                    run.initial_event_sequence = applied_through_sequence
                    run.last_event_sequence = applied_through_sequence
                    self.run_service.add_run(run)
        except Exception:
            self._clear_start_reservation(key, reservation)
            raise

        if conflict_message is not None:
            self._clear_start_reservation(key, reservation)
            return WorkbenchCommandErrorResp(
                id=command_id,
                message=conflict_message,
            )

        if duplicate_run is not None:
            self._clear_start_reservation(key, reservation)
            return WorkbenchCommandSuccessResp.create(
                command_id=command_id,
                run_id=duplicate_run.id,
                applied_through_sequence=applied_through_sequence,
            )

        with self._condition:
            if self._starting_runs.get(key) is reservation:
                self._starting_runs.pop(key, None)
            self._runs[key] = run
            self._condition.notify_all()
        # 3.通知跨进程 SSE 订阅者有新 Run；Redis 故障时仍由本进程条件变量
        # 唤醒，后续再走低频数据库降级检查。
        redis_repository = self.redis_repository
        redis_repository.publish_run_started(
            thread_id=thread_id,
            run_id=run.run_id,
        )
        try:
            self._start_background_run(run, wait_for=previous_run)
        except Exception as exc:
            run.status = WorkbenchRunStatus.FAILED
            run.error_message = str(exc)
            self.run_service.set_status(
                run,
                WorkbenchRunStatus.FAILED,
                error_message=str(exc),
            )
            with self._condition:
                run.done.set()
                if self._runs.get(key) is run:
                    self._runs.pop(key, None)
                self._condition.notify_all()
            raise
        return WorkbenchCommandSuccessResp.create(
            command_id=command_id,
            run_id=run.run_id,
            applied_through_sequence=applied_through_sequence,
        )

    def _clear_start_reservation(
        self,
        key: str,
        reservation: WorkbenchRunStartReservation,
    ) -> None:
        """清理启动占位并唤醒等待同一线程的提交。"""
        with self._condition:
            if self._starting_runs.get(key) is reservation:
                self._starting_runs.pop(key, None)
            self._condition.notify_all()

    # ---------- 后台 Run 启动 ----------

    def _start_background_run(
        self,
        run: WorkbenchRun,
        *,
        wait_for: WorkbenchRun | None = None,
    ) -> None:
        """在 command 所在 worker 启动生产者，SSE 只负责订阅。"""
        flask_app = current_app._get_current_object()

        def execute() -> None:
            with flask_app.app_context():
                try:
                    if wait_for is not None:
                        wait_for.done.wait()
                    # 同一 LangGraph thread 禁止并发写 checkpoint；前置 Run
                    # 结束后再进入 Agent 执行。
                    while self.run_service.has_unfinished_predecessor(run.run_id):
                        time.sleep(self._cancel_signal_poll_interval)
                    persisted_sequence = (
                        self.run_service.get_thread_last_event_sequence(
                            thread_id=run.thread_id,
                            invoke_from=run.spec.invoke_from,
                        )
                    )
                    run.initial_event_sequence = max(
                        run.initial_event_sequence,
                        persisted_sequence,
                    )
                    run.last_event_sequence = run.initial_event_sequence
                    self._produce_run(run)
                except Exception as exc:
                    # 生产线程尚未进入 _produce_run 时也必须把 Run 收敛为失败，
                    # 否则前置 Run 查询或序号查询异常会留下永久 queued 状态。
                    self._fail_background_start(run, exc)
                finally:
                    self.db.session.remove()

        thread = threading.Thread(
            target=execute,
            name=f"agent-workbench-{run.run_id}",
            daemon=True,
        )
        thread.start()

    def _fail_background_start(self, run: WorkbenchRun, exc: Exception) -> None:
        """处理后台初始化失败，保证数据库和本地状态都能结束。"""
        run.status = WorkbenchRunStatus.FAILED
        run.error_message = str(exc)
        try:
            self.run_service.set_status(
                run,
                WorkbenchRunStatus.FAILED,
                error_message=str(exc),
            )
        except Exception:
            current_app.logger.exception("保存 Agent Run 初始化失败状态失败")
        finally:
            with self._condition:
                run.done.set()
                key = str(run.thread_id)
                if self._runs.get(key) is run:
                    self._runs.pop(key, None)
                self._condition.notify_all()
        current_app.logger.exception(
            "Agent 工作台后台初始化失败",
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    # ---------- Agent 执行和事件发布 ----------

    def _publish_event(
        self,
        run: WorkbenchRun,
        raw_event: Mapping[str, Any],
    ) -> None:
        """安全投影并校验事件，然后持久化并通知本进程订阅者。"""
        event = AgentProtocolEventProjector.to_public_event(raw_event)
        if event is None:
            return
        if event.method == "values" and isinstance(event.params.data, dict):
            run.state_snapshot = event.params.data
        run.last_event_sequence = event.seq
        redis_repository = self.redis_repository
        redis_repository.append_event(run.run_id, event)
        self.run_service.record_event(run, event)
        with self._condition:
            run.events.append(event)
            self._condition.notify_all()

    def _produce_run(self, run: WorkbenchRun) -> None:
        """在后台装配并执行 Agent，同时投影和持久化运行事件。"""
        started_at = time.monotonic()
        agent = None
        raw_stream = None
        projector = AgentProtocolEventProjector(
            run.run_id,
            start_sequence=run.initial_event_sequence,
            root_message_id=f"{run.spec.message_id}:assistant",
        )
        try:
            # queued Run 可能在等待前置 Run 时被取消。此时直接收敛为终态，
            # 不应将其标记为 running，也不应初始化 Agent 或写入 LangGraph。
            if self._is_cancelled(run):
                self._finish_cancelled_run(run, projector)
                return
            run.status = WorkbenchRunStatus.RUNNING
            self.run_service.set_status(run, WorkbenchRunStatus.RUNNING)
            self._publish_event(
                run,
                projector.generated(
                    "lifecycle",
                    {"event": "started", "status": "running"},
                ),
            )
            # 2.读取本次 Run 对应的草稿/发布配置，并装配独占 Agent 资源。
            if run.spec.invoke_from is not InvokeFrom.ASSISTANT_AGENT:
                self.hydrate_config(run)
            agent, llm = self.agent_assembly_service.build_agent(run.spec)
            # 3.把用户输入转换为模型消息，消费 DeepAgents v3 原始事件流。
            query = llm.convert_to_human_message(
                run.spec.query,
                run.spec.image_urls,
                dump_agent_attachments(run.spec.attachments),
            )
            if run.spec.input_message_id:
                query.id = run.spec.input_message_id
            raw_stream = agent.stream_events({"messages": [query]})
            for raw_event in raw_stream:
                if self._is_cancelled(run):
                    break
                for event in projector.project_many(raw_event):
                    self._publish_event(run, event)
        except Exception as exc:
            run.status = WorkbenchRunStatus.FAILED
            run.error_message = str(exc)
            for pending_message in projector.finish_pending_messages():
                self._publish_event(run, pending_message)
            failure = projector.generated(
                "lifecycle",
                {
                    "event": "failed",
                    "status": "failed",
                    "error": "Agent 运行失败",
                },
            )
            self._publish_event(run, failure)
            self.run_service.set_status(
                run,
                WorkbenchRunStatus.FAILED,
                error_message=str(exc),
            )
            message = self.get(Message, run.spec.message_id)
            if message is not None:
                with self.db.auto_commit():
                    message.status = MessageStatus.ERROR
                    message.error = "Agent 运行失败"
            current_app.logger.exception("Agent 工作台运行失败")
        else:
            # 4.正常结束时补齐未关闭消息、最终回答、状态快照和 completed 事件。
            if self._is_cancelled(run):
                self._finish_cancelled_run(run, projector)
                return
            for pending_message in projector.finish_pending_messages():
                self._publish_event(run, pending_message)
            self._persist_terminal_message(
                run,
                latency=max(0.0, time.monotonic() - started_at),
                run_usage=agent.last_run_usage if agent is not None else None,
            )
            terminal_values = dict(run.state_snapshot or {})
            terminal_page = self.run_service.get_message_page(thread_id=run.thread_id)
            terminal_values["messages"] = [
                message.model_dump(mode="json") for message in terminal_page.messages
            ]
            terminal_values["message_page"] = terminal_page.page.model_dump(mode="json")
            self._publish_event(
                run,
                projector.generated("values", terminal_values),
            )
            self._publish_event(
                run,
                projector.generated(
                    "lifecycle",
                    {"event": "completed", "status": "completed"},
                ),
            )
            run.status = WorkbenchRunStatus.COMPLETED
            self.run_service.set_status(run, WorkbenchRunStatus.COMPLETED)
        finally:
            close = getattr(raw_stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    current_app.logger.exception("关闭 Agent v3 事件流失败")
            with self._condition:
                run.done.set()
                key = str(run.thread_id)
                if self._runs.get(key) is run:
                    self._runs.pop(key, None)
                self._condition.notify_all()

    def _finish_cancelled_run(
        self,
        run: WorkbenchRun,
        projector: AgentProtocolEventProjector,
    ) -> None:
        """发布取消终态；兼容运行中和排队期间取消两种路径。"""
        run.status = WorkbenchRunStatus.CANCELLED
        for pending_message in projector.finish_pending_messages():
            self._publish_event(run, pending_message)
        cancelled = projector.generated(
            "lifecycle",
            {
                "event": "interrupted",
                "status": "cancelled",
                "reason": "cancel_requested",
            },
        )
        self._publish_event(run, cancelled)
        self.run_service.set_status(run, WorkbenchRunStatus.CANCELLED)
        message = self.get(Message, run.spec.message_id)
        if message is not None:
            with self.db.auto_commit():
                message.status = MessageStatus.STOP

    # ---------- 取消和本地运行状态 ----------

    def _is_cancelled(self, run: WorkbenchRun) -> bool:
        """优先读取快速取消信号，并用数据库状态低频兜底。"""
        if run.cancel_requested.is_set():
            return True
        now = time.monotonic()
        redis_repository = self.redis_repository
        if now - run.last_cancel_signal_check >= self._cancel_signal_poll_interval:
            run.last_cancel_signal_check = now
            remote_cancelled = redis_repository.is_cancel_requested(run.run_id)
            if remote_cancelled is True:
                return True
        if now - run.last_cancel_status_check < self._cancel_status_poll_interval:
            return False
        run.last_cancel_status_check = now
        return self.run_service.is_cancel_requested(run.run_id)

    def _cancel_run_row(
        self,
        row: AgentRun,
        *,
        thread_id: UUID,
        account: Account,
    ) -> bool:
        """对已定位的运行执行鉴权、持久化和跨进程取消通知。"""
        self.access_service.authorize_persisted_run(row, account)
        if row.status in WORKBENCH_TERMINAL_STATUSES:
            return False
        self.run_service.request_cancel(row)
        redis_repository = self.redis_repository
        redis_repository.request_cancel(row.id)
        key = str(thread_id)
        with self._condition:
            pending = self._runs.get(key)
            if pending is not None and pending.run_id == row.id:
                pending.cancel_requested.set()
                pending.status = WorkbenchRunStatus.CANCELLING
            self._condition.notify_all()
        return True

    def cancel_run(
        self,
        *,
        invoke_from: InvokeFrom,
        access_value: str,
        thread_id: UUID,
        run_id: UUID,
        account: Account,
    ) -> WorkbenchCancelResp:
        """按 Agent Protocol 返回的 run_id 取消指定 Run。"""
        self.access_service.authorize_thread(
            invoke_from=invoke_from,
            access_value=access_value,
            thread_id=thread_id,
            account=account,
        )
        row = self.run_service.get_run(run_id)
        if (
            row is None
            or row.thread_id != thread_id
            or row.invoke_from != invoke_from.value
        ):
            self.db.session.close()
            raise NotFoundException("Agent Run 不存在或不属于当前线程")
        cancel_requested = self._cancel_run_row(
            row,
            thread_id=thread_id,
            account=account,
        )
        return WorkbenchCancelResp(
            cancel_requested=cancel_requested,
            run_id=row.id,
            status=(
                WorkbenchRunStatus.CANCELLING.value
                if cancel_requested
                else str(row.status)
            ),
        )

    def _stream_local_run(
        self,
        run: WorkbenchRun,
        *,
        since: int,
        subscription: WorkbenchSubscriptionReq,
    ):
        """订阅本 worker 的有界事件缓冲。"""
        cursor = since
        heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
        # 本地 Run 的事件队列会持续追加事件；生成器直到 Run 完成且队列消费完才结束。
        while True:
            reset_event = None
            heartbeat = False
            with self._condition:
                while (
                    not any(event.seq > cursor for event in run.events)
                    and not run.done.is_set()
                ):
                    remaining = heartbeat_at - time.monotonic()
                    if remaining <= 0:
                        heartbeat = True
                        heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
                        break
                    self._condition.wait(timeout=min(1, remaining))
                if run.events:
                    oldest_event_sequence = run.events[0].seq
                    if cursor > 0 and cursor < oldest_event_sequence - 1:
                        reset_event = self._cursor_reset_event(
                            run,
                            requested_sequence=cursor,
                            oldest_event_sequence=oldest_event_sequence,
                        )
                        cursor = oldest_event_sequence - 1
                pending = [event for event in run.events if event.seq > cursor]
                if pending:
                    cursor = pending[-1].seq
                finished = run.done.is_set() and not pending
            if reset_event is not None:
                yield reset_event.model_dump(mode="json")
            for event in pending:
                if subscription.matches(event):
                    yield event.model_dump(mode="json")
            if heartbeat and not pending:
                yield None
            if finished:
                return

    def _subscribe_events(
        self,
        *,
        invoke_from: InvokeFrom,
        thread_id: UUID,
        subscription: WorkbenchSubscriptionReq,
    ):
        """订阅工作台事件；跨轮等待使用通知，不再用数据库高频轮询。

        该生成器在 Run 之间持续等待，因此这里的无限循环是订阅生命周期，
        不是无条件重试；客户端断开或生成器关闭时由 ``finally`` 释放资源。
        """
        # SSE 生成器可能在请求上下文销毁后才被关闭；保存当前 Session 实例，
        # finally 直接关闭它，避免再次访问 Flask-SQLAlchemy 的 scopefunc。
        stream_session = self.db.session()
        key = str(thread_id)
        since = subscription.since
        consumed_run_id: str | None = None
        heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
        redis_repository = self.redis_repository
        pubsub = redis_repository.subscribe_thread(thread_id)
        next_db_check_at = 0.0
        try:
            # 一个线程可以连续提交多个 Run，订阅必须跨 Run 保持连接。
            while True:
                with self._condition:
                    run = self._runs.get(key)
                if run is not None and str(run.run_id) != consumed_run_id:
                    yield from self._stream_local_run(
                        run,
                        since=since,
                        subscription=subscription,
                    )
                    since = max(since, int(run.last_event_sequence or 0))
                    consumed_run_id = str(run.run_id)
                    heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
                    continue

                # 连接初始化时读取一次数据库，后续由 Redis 通知发现新 Run。
                now = time.monotonic()
                should_check_db = pubsub is None and now >= next_db_check_at
                if consumed_run_id is None and pubsub is not None:
                    should_check_db = True
                if should_check_db:
                    db_run = self.run_service.get_latest_run(
                        thread_id=thread_id,
                        invoke_from=invoke_from,
                    )
                    next_db_check_at = now + self._db_poll_interval
                    if db_run is not None and str(db_run.id) != consumed_run_id:
                        yield from self._stream_db_run(
                            db_run,
                            since=since,
                            subscription=subscription,
                        )
                        since = max(since, int(db_run.last_event_sequence or 0))
                        consumed_run_id = str(db_run.id)
                        continue

                remaining = heartbeat_at - time.monotonic()
                if remaining <= 0:
                    heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
                    yield None

                notified_run_id = None
                if pubsub is not None:
                    # Redis PubSub 阻塞等待新 Run，不再每 250ms 查询数据库。
                    try:
                        message = pubsub.get_message(
                            ignore_subscribe_messages=True,
                            timeout=min(1.0, max(0.1, remaining)),
                        )
                        if isinstance(message, Mapping):
                            raw_run_id = message.get("data")
                            # redis-py 未开启 decode_responses 时，消息正文是 bytes。
                            if isinstance(raw_run_id, bytes):
                                notified_run_id = raw_run_id.decode("utf-8")
                            else:
                                notified_run_id = str(raw_run_id or "")
                    except (RedisError, OSError, TypeError, ValueError):
                        # PubSub 断开后立即切换到本进程条件变量和低频数据库降级。
                        try:
                            pubsub.close()
                        except (RedisError, OSError):
                            pass
                        pubsub = None
                        next_db_check_at = 0.0
                else:
                    # Redis 故障时保留本进程唤醒，并使用低频数据库降级检查。
                    with self._condition:
                        self._condition.wait(timeout=min(1.0, max(0.1, remaining)))

                if not notified_run_id:
                    continue

                # 通知只携带 run_id，详细事件仍从本地缓冲或 Redis Stream 读取。
                with self._condition:
                    notified_run = self._runs.get(key)
                if (
                    notified_run is not None
                    and str(notified_run.run_id) == notified_run_id
                ):
                    continue
                db_run = self.run_service.get_latest_run(
                    thread_id=thread_id,
                    invoke_from=invoke_from,
                )
                if db_run is not None and str(db_run.id) == notified_run_id:
                    yield from self._stream_db_run(
                        db_run,
                        since=since,
                        subscription=subscription,
                    )
                    since = max(since, int(db_run.last_event_sequence or 0))
                    consumed_run_id = str(db_run.id)
        finally:
            # 刷新、断开或异常时释放 Redis PubSub 和数据库 Session。
            if pubsub is not None:
                try:
                    pubsub.close()
                except (RedisError, OSError):
                    pass
            stream_session.close()

    def _persist_terminal_message(
        self,
        run: WorkbenchRun,
        *,
        latency: float,
        run_usage: Mapping[str, Any] | None,
    ) -> None:
        """回写最终回答和本轮模型用量，不重复持久化思考轨迹。"""
        state_snapshot = run.state_snapshot or {}
        messages = (
            state_snapshot.get("messages")
            if isinstance(state_snapshot, Mapping)
            else None
        )
        answer = ""
        if isinstance(messages, list):
            for item in reversed(messages):
                if not isinstance(item, Mapping):
                    continue
                if item.get("type") not in {"ai", "assistant"}:
                    continue
                answer = message_content_to_text(item.get("content"))
                if answer:
                    break
        if not answer:
            return
        message = self.get(Message, run.spec.message_id)
        if message is None:
            return
        with self.db.auto_commit():
            message.answer = answer
            message.status = MessageStatus.NORMAL
            message.latency = latency
            for field, value in ConversationService.build_run_usage_updates(
                run_usage
            ).items():
                setattr(message, field, value)


__all__ = ["AgentWorkbenchService"]
