"""工作台 Run 的入口适配和运行请求构造。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from injector import inject

from internal.entity.conversation_entity import InvokeFrom, MessageStatus
from internal.entity import (
    AuthorizedWorkbenchContext,
    WorkbenchRun,
    WorkbenchRunInput,
)
from internal.exception import ForbiddenException
from internal.model import Account, Conversation, EndUser, Message
from internal.schema.agent_attachment_schema import (
    AgentAttachmentResolveReq,
    dump_agent_attachments,
)
from internal.schema.agent_runtime_schema import AgentInvocation
from internal.schema.app_config_schema import AgentAppConfigReq
from pkg.sqlalchemy import SQLAlchemy

from .agent_attachment_service import AgentAttachmentService
from .assistant_agent_service import AssistantAgentService
from .base_service import BaseService


@inject
@dataclass
class AgentWorkbenchRunFactory(BaseService):
    """根据不同 Agent Protocol 入口创建一次性 WorkbenchRun。"""

    db: SQLAlchemy
    attachment_service: AgentAttachmentService
    assistant_agent_service: AssistantAgentService

    def create_run(
        self,
        *,
        invoke_from: InvokeFrom,
        access_value: str,
        thread_id: UUID,
        account: Account,
        run_input: WorkbenchRunInput,
        run_id: UUID,
        authorized: AuthorizedWorkbenchContext,
    ) -> WorkbenchRun:
        """按调用来源创建 Run 及其 AgentInvocation。"""
        if invoke_from is InvokeFrom.DEBUGGER:
            return self._create_debug_run(
                app_id=UUID(access_value),
                thread_id=thread_id,
                account=account,
                run_input=run_input,
                run_id=run_id,
                authorized=authorized,
            )
        if invoke_from is InvokeFrom.WEB_APP:
            return self._create_web_app_run(
                thread_id=thread_id,
                account=account,
                run_input=run_input,
                run_id=run_id,
                authorized=authorized,
            )
        if invoke_from is InvokeFrom.CREATOR_TASK:
            return self._create_creator_task_run(
                app_id=UUID(access_value),
                thread_id=thread_id,
                account=account,
                run_input=run_input,
                run_id=run_id,
                authorized=authorized,
            )
        if invoke_from is InvokeFrom.ASSISTANT_AGENT:
            return self._create_assistant_run(
                thread_id=thread_id,
                account=account,
                run_input=run_input,
                run_id=run_id,
                authorized=authorized,
            )
        if invoke_from is InvokeFrom.SERVICE_API:
            return self._create_service_api_run(
                app_id=UUID(access_value),
                thread_id=thread_id,
                account=account,
                run_input=run_input,
                run_id=run_id,
                authorized=authorized,
            )
        raise ValueError("不支持的工作台调用来源")

    def create_from_invocation(
        self,
        *,
        invocation: AgentInvocation,
        thread_id: UUID,
        run_id: UUID,
    ) -> WorkbenchRun:
        """把已准备好的内部调用包装为 WorkbenchRun。"""
        return WorkbenchRun(
            run_id=run_id,
            thread_id=thread_id,
            spec=invocation,
        )

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

    def _create_debug_run(
        self,
        *,
        app_id: UUID,
        thread_id: UUID,
        account: Account,
        run_input: WorkbenchRunInput,
        run_id: UUID,
        authorized: AuthorizedWorkbenchContext,
    ) -> WorkbenchRun:
        """创建调试入口的消息和 WorkbenchRun。"""
        app = authorized.app
        conversation = authorized.conversation
        if app is None or conversation is None:
            raise ForbiddenException("工作台授权上下文不完整")
        include_opening_statement = self._is_first_message(conversation.id)
        conversation.updated_at = datetime.now()
        attachments = self._resolve_attachments(run_input, owner_id=account.id)
        message = Message(
            id=uuid4(),
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
        request = AgentInvocation(
            account_id=account.id,
            configuration_account_id=account.id,
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
            selected_skills=run_input.selected_skills,
            input_message_id=run_input.input_message_id,
        )
        return WorkbenchRun(
            run_id=run_id,
            thread_id=thread_id,
            spec=request,
            include_opening_statement=include_opening_statement,
        )

    def _create_web_app_run(
        self,
        *,
        thread_id: UUID,
        account: Account,
        run_input: WorkbenchRunInput,
        run_id: UUID,
        authorized: AuthorizedWorkbenchContext,
    ) -> WorkbenchRun:
        """创建 WebApp 入口的消息和 WorkbenchRun。"""
        app = authorized.app
        end_user = authorized.end_user
        conversation = authorized.conversation
        if app is None or end_user is None:
            raise ForbiddenException("WebApp 授权上下文不完整")
        if conversation is None:
            conversation = self.db.session.get(Conversation, thread_id)
            if conversation is None:
                conversation = Conversation(
                    id=thread_id,
                    app_id=app.id,
                    name=run_input.query[:255],
                    invoke_from=InvokeFrom.WEB_APP,
                    created_by=end_user.id,
                    owner_account_id=account.id,
                )
                self.db.session.add(conversation)
            elif (
                conversation.app_id != app.id
                or conversation.invoke_from
                not in {InvokeFrom.WEB_APP, InvokeFrom.CREATOR_TASK}
                or conversation.created_by not in {end_user.id, account.id}
                or conversation.is_deleted is True
            ):
                raise ForbiddenException("会话不存在或不属于当前用户")
            elif conversation.owner_account_id is None:
                conversation.owner_account_id = account.id
        include_opening_statement = self._is_first_message(conversation.id)
        conversation.updated_at = datetime.now()
        attachments = self._resolve_attachments(run_input, owner_id=account.id)
        message = Message(
            id=uuid4(),
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
        request = AgentInvocation(
            account_id=account.id,
            configuration_account_id=app.account_id,
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
            selected_skills=run_input.selected_skills,
            input_message_id=run_input.input_message_id,
        )
        return WorkbenchRun(
            run_id=run_id,
            thread_id=thread_id,
            spec=request,
            include_opening_statement=include_opening_statement,
        )

    def _create_creator_task_run(
        self,
        *,
        app_id: UUID,
        thread_id: UUID,
        account: Account,
        run_input: WorkbenchRunInput,
        run_id: UUID,
        authorized: AuthorizedWorkbenchContext,
    ) -> WorkbenchRun:
        """创建任务中心入口的消息和 WorkbenchRun。

        Creator 直接使用已发布配置，但会话归属写入账号维度，因而可以
        与同账号通过 WebApp 产生的历史会话互相展示。
        """
        app = authorized.app
        conversation = authorized.conversation
        if app is None:
            raise ForbiddenException("Creator 任务授权上下文不完整")
        if conversation is None:
            conversation = Conversation(
                id=thread_id,
                app_id=app.id,
                name=run_input.query[:255],
                invoke_from=InvokeFrom.CREATOR_TASK,
                created_by=account.id,
                owner_account_id=account.id,
            )
            self.db.session.add(conversation)
        elif conversation.is_deleted:
            raise ForbiddenException("会话不存在或已删除")
        elif conversation.owner_account_id is None:
            conversation.owner_account_id = account.id

        include_opening_statement = self._is_first_message(conversation.id)
        conversation.updated_at = datetime.now()
        attachments = self._resolve_attachments(run_input, owner_id=account.id)
        message = Message(
            id=uuid4(),
            app_id=app.id,
            conversation_id=conversation.id,
            invoke_from=InvokeFrom.CREATOR_TASK,
            created_by=account.id,
            query=run_input.query,
            image_urls=list(run_input.image_urls),
            attachments=dump_agent_attachments(attachments),
            status=MessageStatus.NORMAL,
        )
        self.db.session.add(message)
        request = AgentInvocation(
            account_id=account.id,
            configuration_account_id=account.id,
            app_id=app.id,
            app_name=app.name,
            app_description=app.description,
            app_config=AgentAppConfigReq(),
            conversation_id=conversation.id,
            message_id=message.id,
            created_by=account.id,
            user_id=account.id,
            end_user_id=None,
            invoke_from=InvokeFrom.CREATOR_TASK,
            query=run_input.query,
            image_urls=run_input.image_urls,
            attachments=attachments,
            selected_skills=run_input.selected_skills,
            input_message_id=run_input.input_message_id,
        )
        return WorkbenchRun(
            run_id=run_id,
            thread_id=thread_id,
            spec=request,
            include_opening_statement=include_opening_statement,
        )

    def _create_assistant_run(
        self,
        *,
        thread_id: UUID,
        account: Account,
        run_input: WorkbenchRunInput,
        run_id: UUID,
        authorized: AuthorizedWorkbenchContext,
    ) -> WorkbenchRun:
        """创建 Creator 辅助 Agent 入口的 WorkbenchRun。"""
        conversation = authorized.conversation
        if conversation is None:
            raise ForbiddenException("辅助 Agent 授权上下文不完整")
        if conversation.id != thread_id:
            raise ForbiddenException("辅助 Agent 线程不存在或无权访问")
        attachments = self._resolve_attachments(run_input, owner_id=account.id)
        message = Message(
            id=uuid4(),
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
        llm, tools = self.assistant_agent_service.prepare_runtime(account)
        request = AgentInvocation(
            account_id=account.id,
            configuration_account_id=account.id,
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
            selected_skills=run_input.selected_skills,
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
        authorized: AuthorizedWorkbenchContext,
    ) -> WorkbenchRun:
        """创建 Service API 入口的消息和 WorkbenchRun。"""
        app = authorized.app
        conversation = authorized.conversation
        if app is None:
            raise ForbiddenException("服务 API 授权上下文不完整")
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
            end_user = EndUser(id=uuid4(), tenant_id=account.id, app_id=app.id)
            self.db.session.add(end_user)
        if conversation is None:
            conversation = Conversation(
                id=thread_id,
                app_id=app.id,
                name=run_input.query[:255],
                invoke_from=InvokeFrom.SERVICE_API,
                created_by=end_user.id,
            )
            self.db.session.add(conversation)
        elif end_user is None or conversation.created_by != end_user.id:
            raise ForbiddenException("服务 API 线程终端用户无效")
        include_opening_statement = self._is_first_message(conversation.id)
        attachments = self._resolve_attachments(run_input, owner_id=account.id)
        message = Message(
            id=uuid4(),
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
        request = AgentInvocation(
            account_id=account.id,
            configuration_account_id=account.id,
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
            selected_skills=run_input.selected_skills,
            input_message_id=run_input.input_message_id,
        )
        return WorkbenchRun(
            run_id=run_id,
            thread_id=thread_id,
            spec=request,
            include_opening_statement=include_opening_statement,
        )

    def _is_first_message(self, conversation_id: UUID) -> bool:
        """使用存在性查询判断开场白，避免 Conversation.is_new 的 COUNT。"""
        return (
            self.db.session.query(Message.id)
            .filter(
                Message.conversation_id == conversation_id,
                Message.is_deleted.is_(False),
            )
            .first()
            is None
        )


__all__ = ["AgentWorkbenchRunFactory"]
