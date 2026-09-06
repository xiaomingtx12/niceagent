"""工作台 Run 的入口适配和运行请求构造。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from injector import inject

from internal.entity import (
    AgentAttachment,
    AuthorizedWorkbenchContext,
    WorkbenchRun,
    WorkbenchRunInput,
)
from internal.entity.conversation_entity import InvokeFrom, MessageStatus
from internal.exception import ForbiddenException
from internal.model import Account, Conversation, EndUser, Message
from internal.schema.agent_attachment_schema import (
    AgentAttachmentResolveReq,
    dump_agent_attachments,
)
from internal.schema.agent_runtime_schema import AgentInvocation
from pkg.sqlalchemy import SQLAlchemy

from .agent_attachment_service import AgentAttachmentService
from .base_service import BaseService


@inject
@dataclass
class AgentWorkbenchRunFactory(BaseService):
    """根据不同 Agent Protocol 入口创建一次性 WorkbenchRun。"""

    db: SQLAlchemy
    attachment_service: AgentAttachmentService

    # -------------------- Public Run construction API --------------------

    def create_run(
        self,
        *,
        invoke_from: InvokeFrom,
        account: Account,
        run_input: WorkbenchRunInput,
        run_id: UUID,
        authorized: AuthorizedWorkbenchContext,
    ) -> WorkbenchRun:
        """按调用来源创建 Run 及其 AgentInvocation。"""
        # 1.按入口选择专属的业务归属和消息创建策略。
        if invoke_from is InvokeFrom.DEBUGGER:
            return self._create_debug_run(
                account=account,
                run_input=run_input,
                run_id=run_id,
                authorized=authorized,
            )
        if invoke_from is InvokeFrom.WEB_APP:
            return self._create_web_app_run(
                account=account,
                run_input=run_input,
                run_id=run_id,
                authorized=authorized,
            )
        if invoke_from is InvokeFrom.CREATOR_TASK:
            return self._create_creator_task_run(
                account=account,
                run_input=run_input,
                run_id=run_id,
                authorized=authorized,
            )
        if invoke_from is InvokeFrom.ASSISTANT_AGENT:
            return self._create_assistant_run(
                account=account,
                run_input=run_input,
                run_id=run_id,
                authorized=authorized,
            )
        if invoke_from is InvokeFrom.SERVICE_API:
            return self._create_service_api_run(
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
        run_id: UUID,
    ) -> WorkbenchRun:
        """把已准备好的内部调用包装为 WorkbenchRun。"""
        # 1.内部调用已经完成输入和归属校验，只需包装为统一 Run 实体。
        return WorkbenchRun(
            run_id=run_id,
            conversation_id=invocation.conversation_id,
            spec=invocation,
        )

    # -------------------- Entry-specific builders --------------------

    def _create_debug_run(
        self,
        *,
        account: Account,
        run_input: WorkbenchRunInput,
        run_id: UUID,
        authorized: AuthorizedWorkbenchContext,
    ) -> WorkbenchRun:
        """创建调试入口的消息和 WorkbenchRun。"""
        # 1.复用鉴权上下文更新会话并解析附件。
        app = authorized.app
        conversation = authorized.conversation
        if app is None or conversation is None:
            raise ForbiddenException("工作台授权上下文不完整")
        attachments = self._resolve_attachments(run_input, owner_id=account.id)
        # 2.统一创建业务 Message 和 AgentInvocation。
        return self._build_run(
            run_id=run_id,
            account_id=account.id,
            app_id=app.id,
            conversation=conversation,
            created_by=account.id,
            end_user_id=None,
            invoke_from=InvokeFrom.DEBUGGER,
            run_input=run_input,
            attachments=attachments,
        )

    def _create_web_app_run(
        self,
        *,
        account: Account,
        run_input: WorkbenchRunInput,
        run_id: UUID,
        authorized: AuthorizedWorkbenchContext,
    ) -> WorkbenchRun:
        """创建 WebApp 入口的消息和 WorkbenchRun。"""
        # 1.复用已鉴权的 WebApp 会话。
        app = authorized.app
        end_user = authorized.end_user
        conversation = authorized.conversation
        if app is None or end_user is None or conversation is None:
            raise ForbiddenException("WebApp 授权上下文不完整")
        if (
            conversation.app_id != app.id
            or conversation.invoke_from
            not in {InvokeFrom.WEB_APP, InvokeFrom.CREATOR_TASK}
            or conversation.created_by not in {end_user.id, account.id}
            or conversation.is_deleted is True
        ):
            raise ForbiddenException("会话不存在或不属于当前用户")
        attachments = self._resolve_attachments(run_input, owner_id=account.id)
        # 2.统一创建业务 Message 和 AgentInvocation。
        return self._build_run(
            run_id=run_id,
            account_id=account.id,
            app_id=app.id,
            conversation=conversation,
            created_by=end_user.id,
            end_user_id=end_user.id,
            invoke_from=InvokeFrom.WEB_APP,
            run_input=run_input,
            attachments=attachments,
        )

    def _create_creator_task_run(
        self,
        *,
        account: Account,
        run_input: WorkbenchRunInput,
        run_id: UUID,
        authorized: AuthorizedWorkbenchContext,
    ) -> WorkbenchRun:
        """创建任务中心入口的消息和 WorkbenchRun。

        Creator 直接使用已发布配置，但会话归属写入账号维度，因而可以
        与同账号通过 WebApp 产生的历史会话互相展示。
        """
        # 1.复用已鉴权的 Creator 任务会话。
        app = authorized.app
        conversation = authorized.conversation
        if app is None or conversation is None:
            raise ForbiddenException("Creator 任务授权上下文不完整")
        if conversation.is_deleted:
            raise ForbiddenException("会话不存在或已删除")
        elif conversation.owner_account_id is None:
            conversation.owner_account_id = account.id

        attachments = self._resolve_attachments(run_input, owner_id=account.id)
        # 2.统一创建业务 Message 和 AgentInvocation。
        return self._build_run(
            run_id=run_id,
            account_id=account.id,
            app_id=app.id,
            conversation=conversation,
            created_by=account.id,
            end_user_id=None,
            invoke_from=InvokeFrom.CREATOR_TASK,
            run_input=run_input,
            attachments=attachments,
        )

    def _create_assistant_run(
        self,
        *,
        account: Account,
        run_input: WorkbenchRunInput,
        run_id: UUID,
        authorized: AuthorizedWorkbenchContext,
    ) -> WorkbenchRun:
        """创建 Creator 辅助 Agent 入口的 WorkbenchRun。"""
        # 1.校验辅助 Agent 会话并解析当前用户附件。
        conversation = authorized.conversation
        if conversation is None:
            raise ForbiddenException("辅助 Agent 授权上下文不完整")
        attachments = self._resolve_attachments(run_input, owner_id=account.id)
        # 2.统一创建业务 Message 和 AgentInvocation。
        return self._build_run(
            run_id=run_id,
            account_id=account.id,
            app_id=conversation.app_id,
            conversation=conversation,
            created_by=account.id,
            end_user_id=None,
            invoke_from=InvokeFrom.ASSISTANT_AGENT,
            run_input=run_input,
            attachments=attachments,
        )

    def _create_service_api_run(
        self,
        *,
        account: Account,
        run_input: WorkbenchRunInput,
        run_id: UUID,
        authorized: AuthorizedWorkbenchContext,
    ) -> WorkbenchRun:
        """创建 Service API 入口的消息和 WorkbenchRun。"""
        # 1.解析 Service API 终端用户和会话归属。
        app = authorized.app
        conversation = authorized.conversation
        if app is None or conversation is None:
            raise ForbiddenException("服务 API 授权上下文不完整")
        if (
            conversation.app_id != app.id
            or conversation.invoke_from != InvokeFrom.SERVICE_API
        ):
            raise ForbiddenException("服务 API 会话不属于当前应用")
        end_user = (
            self.db.session.query(EndUser)
            .filter(
                EndUser.id == conversation.created_by,
                EndUser.app_id == app.id,
                EndUser.tenant_id == account.id,
            )
            .one_or_none()
        )
        if end_user is None:
            raise ForbiddenException("服务 API 会话终端用户无效")
        attachments = self._resolve_attachments(run_input, owner_id=account.id)
        # 2.统一创建业务 Message 和 AgentInvocation。
        return self._build_run(
            run_id=run_id,
            account_id=account.id,
            app_id=app.id,
            conversation=conversation,
            created_by=end_user.id,
            end_user_id=end_user.id,
            invoke_from=InvokeFrom.SERVICE_API,
            run_input=run_input,
            attachments=attachments,
        )

    def _build_run(
        self,
        *,
        run_id: UUID,
        account_id: UUID,
        app_id: UUID,
        conversation: Conversation,
        created_by: UUID,
        end_user_id: UUID | None,
        invoke_from: InvokeFrom,
        run_input: WorkbenchRunInput,
        attachments: tuple[AgentAttachment, ...],
    ) -> WorkbenchRun:
        """统一创建消息和 AgentInvocation，保持各入口字段一致。"""
        # 1.后端预创建的空会话在首条消息提交时才获得业务名称。
        is_first_message = (
            self.db.session.query(Message.id)
            .filter(
                Message.conversation_id == conversation.id,
                Message.is_deleted.is_(False),
            )
            .first()
            is None
        )
        if is_first_message:
            conversation.name = run_input.query[:255]
        conversation.updated_at = datetime.now()

        # 2.创建平台业务消息，保留客户端附件的稳定快照。
        message = Message(
            id=uuid4(),
            app_id=app_id,
            conversation_id=conversation.id,
            invoke_from=invoke_from,
            created_by=created_by,
            query=run_input.query,
            image_urls=list(run_input.image_urls),
            attachments=dump_agent_attachments(attachments),
            status=MessageStatus.NORMAL,
        )
        self.db.session.add(message)
        # 3.创建交给 Agent 装配层的不可变 Invocation。
        request = AgentInvocation(
            account_id=account_id,
            app_id=app_id,
            conversation_id=conversation.id,
            message_id=message.id,
            created_by=created_by,
            end_user_id=end_user_id,
            invoke_from=invoke_from,
            query=run_input.query,
            image_urls=run_input.image_urls,
            attachments=attachments,
            selected_skills=run_input.selected_skills,
            input_message_id=run_input.input_message_id,
        )
        return WorkbenchRun(
            run_id=run_id,
            conversation_id=conversation.id,
            spec=request,
        )

    # -------------------- Internal construction helpers --------------------

    def _resolve_attachments(
        self,
        run_input: WorkbenchRunInput,
        *,
        owner_id: UUID,
    ) -> tuple[AgentAttachment, ...]:
        """解析并校验本次 Run 引用的附件。"""
        # 1.校验附件归属并按提交顺序转成不可变集合。
        return tuple(
            self.attachment_service.resolve(
                AgentAttachmentResolveReq.from_values(
                    run_input.file_ids,
                    owner_id=owner_id,
                )
            )
        )


__all__ = ["AgentWorkbenchRunFactory"]
