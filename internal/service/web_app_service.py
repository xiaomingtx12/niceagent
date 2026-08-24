import json
from dataclasses import dataclass
from datetime import datetime
from typing import Generator, Any
from uuid import UUID

from injector import inject
from sqlalchemy import desc
from sqlalchemy.orm import joinedload
from sqlalchemy.dialects.postgresql import insert

from internal.core.agent_v2.agents import AgentQueueManager
from internal.entity.app_entity import AppStatus
from internal.entity.conversation_entity import InvokeFrom, MessageStatus
from internal.exception import NotFoundException, ForbiddenException
from internal.model import App, Account, Conversation, EndUser, Message
from internal.schema.web_app_schema import WebAppChatReq
from internal.schema.conversation_schema import GetConversationMessagesWithPageReq
from pkg.paginator import Paginator
from pkg.sqlalchemy import SQLAlchemy
from .app_config_service import AppConfigService
from .agent_conversation_cleanup_service import AgentConversationCleanupService
from .agent_invocation_service import AgentInvocationService, AgentInvocationSpec
from .agent_attachment_service import AgentAttachmentService
from .base_service import BaseService


@inject
@dataclass
class WebAppService(BaseService):
    """WebApp 入口服务，负责用户、会话和 Agent 调用上下文。"""

    db: SQLAlchemy
    app_config_service: AppConfigService
    agent_invocation_service: AgentInvocationService
    agent_cleanup_service: AgentConversationCleanupService
    agent_attachment_service: AgentAttachmentService

    def resolve_web_app_end_user(
        self,
        *,
        app: App,
        account: Account,
    ) -> EndUser:
        """把已认证的 WebApp 账号解析为应用作用域内的终端用户。"""
        # 1.优先读取当前账号在应用下已有的终端用户映射。
        end_user = (
            self.db.session.query(EndUser)
            .filter(
                EndUser.app_id == app.id,
                EndUser.external_user_id == account.id,
            )
            .one_or_none()
        )

        if end_user is not None:
            if end_user.tenant_id != app.account_id:
                raise ForbiddenException("终端用户不属于当前应用")
            return end_user

        # 2.首次访问使用数据库唯一约束处理并发创建。
        # WebApp 页面会并发加载置顶和普通会话列表。两个请求可能同时
        # 发现终端用户不存在，因此必须让数据库负责处理首次创建竞态。
        statement = (
            insert(EndUser)
            .values(
                tenant_id=app.account_id,
                app_id=app.id,
                external_user_id=account.id,
            )
            .on_conflict_do_nothing(
                index_elements=[EndUser.app_id, EndUser.external_user_id]
            )
        )
        self.db.session.execute(statement)
        self.db.session.commit()

        # 3.插入或冲突后重新查询，返回数据库中的真实记录。
        # 插入成功或发生冲突后统一重新查询，确保返回数据库中的真实记录。
        end_user = (
            self.db.session.query(EndUser)
            .filter(
                EndUser.app_id == app.id,
                EndUser.external_user_id == account.id,
            )
            .one_or_none()
        )
        if end_user is None:
            raise NotFoundException("无法初始化WebApp终端用户")
        if end_user.tenant_id != app.account_id:
            raise ForbiddenException("终端用户不属于当前应用")
        return end_user

    def get_owned_web_conversation(
        self,
        *,
        token: str,
        conversation_id: UUID,
        account: Account,
    ) -> tuple[App, EndUser, Conversation]:
        """获取当前登录账号可访问的 WebApp 会话及其终端用户。"""
        # 1.校验 WebApp token 并解析当前账号对应的终端用户。
        app = self.get_web_app(token)
        end_user = self.resolve_web_app_end_user(app=app, account=account)
        # 2.按应用、调用方式和创建者限制会话可见范围。
        conversation = (
            self.db.session.query(Conversation)
            .filter(
                Conversation.id == conversation_id,
                Conversation.app_id == app.id,
                Conversation.invoke_from == InvokeFrom.WEB_APP,
                Conversation.created_by.in_([end_user.id, account.id]),
                Conversation.is_deleted.is_(False),
            )
            .one_or_none()
        )
        if conversation is None:
            raise ForbiddenException("会话不存在或不属于当前用户")
        return app, end_user, conversation

    def get_or_create_owned_web_conversation(
        self,
        *,
        token: str,
        conversation_id: UUID,
        account: Account,
        ) -> tuple[App, EndUser, Conversation]:
        """获取工作台会话；客户端新 thread 在首次 command 时落库。"""
        # 1.校验作用域并解析 WebApp 终端用户。
        app = self.get_web_app(token)
        end_user = self.resolve_web_app_end_user(app=app, account=account)
        # 2.已有 thread 只允许返回属于当前应用和用户的会话。
        existing = self.get(Conversation, conversation_id)
        if existing is not None:
            if (
                existing.app_id != app.id
                or existing.invoke_from != InvokeFrom.WEB_APP
                or existing.created_by not in {end_user.id, account.id}
                or existing.is_deleted is True
            ):
                raise ForbiddenException("会话不存在或不属于当前用户")
            return app, end_user, existing

        # 3.首次 command 时创建对应的业务会话记录。
        conversation = self.create(
            Conversation,
            id=conversation_id,
            app_id=app.id,
            name="新任务",
            invoke_from=InvokeFrom.WEB_APP,
            created_by=end_user.id,
        )
        return app, end_user, conversation

    def get_web_app(self, token: str) -> App:
        """根据访问令牌获取已发布的 WebApp 应用。"""
        # 1.在数据库中查询令牌对应的应用。
        app = (
            self.db.session.query(App)
            .filter(
                App.token == token,
            )
            .one_or_none()
        )
        if not app or app.status != AppStatus.PUBLISHED:
            raise NotFoundException("该WebApp不存在或者未发布，请核实后重试")

        # 2.返回校验通过的应用。
        return app

    def get_web_app_info(self, token: str) -> dict[str, Any]:
        """根据访问令牌获取 WebApp 的基础信息和运行配置。"""
        # 1.获取应用基础信息。
        app = self.get_web_app(token)

        # 2.获取应用配置。
        app_config = self.app_config_service.get_app_config(app)
        
        # 3.从模型配置中读取输入模态，不实际加载模型实例。
        model_config = app_config.get("model_config", {})
        input_modalities = model_config.get("input_modalities", ["text"])
        if not isinstance(input_modalities, list):
            input_modalities = ["text"]

        # 4.提取接口需要的信息并返回。
        return {
            "id": str(app.id),
            "icon": app.icon,
            "name": app.name,
            "description": app.description,
            "app_config": {
                "opening_statement": app_config.get("opening_statement"),
                "opening_questions": app_config.get("opening_questions"),
                "suggested_after_answer": app_config.get("suggested_after_answer"),
                "input_modalities": input_modalities,
                "native_file_input": bool(
                    model_config.get("native_file_input", False)
                ),
                "text_to_speech": app_config.get("text_to_speech"),
                "speech_to_text": app_config.get("speech_to_text"),
            },
        }

    def web_app_chat(
        self, token: str, req: WebAppChatReq, account: Account
    ) -> Generator:
        """根据传递的token凭证+请求与指定的WebApp进行对话"""
        # 1.获取WebApp应用并校验应用是否发布
        app = self.get_web_app(token)
        end_user = self.resolve_web_app_end_user(app=app, account=account)

        # 2.检测是否传递了会话id，如果传递了需要校验会话的归属信息
        if req.conversation_id.data:
            conversation = self.get(Conversation, req.conversation_id.data)
            if (
                not conversation
                or conversation.app_id != app.id
                or conversation.invoke_from != InvokeFrom.WEB_APP
                or conversation.created_by not in {end_user.id, account.id}
                or conversation.is_deleted is True
            ):
                raise ForbiddenException(
                    "该会话不存在，或者不属于当前应用/用户/调用方式"
                )
        else:
            # 3.如果没传递conversation_id表示新会话，这时候需要创建一个会话
            conversation = self.create(
                Conversation,
                **{
                    "app_id": app.id,
                    "name": "New Conversation",
                    "invoke_from": InvokeFrom.WEB_APP,
                    "created_by": end_user.id,
                },
            )

        # 4.获取校验后的运行时配置
        app_config = self.app_config_service.get_app_config(app)
        attachments = self.agent_attachment_service.resolve(
            req.file_ids.data,
            owner_id=account.id,
        )

        # 5.新建一条消息记录
        message = self.create(
            Message,
            app_id=app.id,
            conversation_id=conversation.id,
            invoke_from=InvokeFrom.WEB_APP,
            created_by=end_user.id,
            query=req.query.data,
            image_urls=req.image_urls.data,
            attachments=[attachment.to_dict() for attachment in attachments],
            status=MessageStatus.NORMAL,
        )

        spec = AgentInvocationSpec(
            account_id=app.account_id,
            app=app,
            app_config=app_config,
            conversation_id=conversation.id,
            message_id=message.id,
            created_by=end_user.id,
            user_id=end_user.id,
            end_user_id=end_user.id,
            invoke_from=InvokeFrom.WEB_APP,
            query=req.query.data,
            image_urls=req.image_urls.data,
            attachments=attachments,
            opening_statement=(
                app_config["opening_statement"] if conversation.is_new else ""
            ),
        )

        # stream() 会把模型/API Key/工具装配异常转换为统一的 SSE error 事件。
        agent_stream = self.agent_invocation_service.stream(spec)

        def generate() -> Generator:
            for agent_thought in agent_stream:
                data = self.agent_invocation_service.serialize_event(
                    agent_thought,
                    conversation_id=conversation.id,
                    message_id=message.id,
                )
                yield f"event: {agent_thought.event.value}\ndata:{json.dumps(data)}\n\n"

        return generate()

    def stop_web_app_chat(self, token: str, task_id: UUID, account: Account):
        """根据访问令牌和任务 ID 停止指定 WebApp 对话。"""
        # 1.获取 WebApp 应用并校验是否已发布。
        app = self.get_web_app(token)
        end_user = self.resolve_web_app_end_user(app=app, account=account)

        # 2.调用 Agent 队列管理器停止指定任务。
        AgentQueueManager.set_stop_flag(
            task_id,
            InvokeFrom.WEB_APP,
            end_user.id,
        )

    def get_conversations(
        self, token: str, is_pinned: bool, account: Account
    ) -> list[Conversation]:
        """获取当前账号在指定 WebApp 下的会话列表。"""
        # 1.获取 WebApp 应用并校验是否已发布。
        app = self.get_web_app(token)
        end_user = self.resolve_web_app_end_user(app=app, account=account)

        # 2.按用户和置顶状态筛选会话并查询数据。
        conversations = (
            self.db.session.query(Conversation)
            .filter(
                Conversation.app_id == app.id,
                Conversation.created_by.in_([end_user.id, account.id]),
                Conversation.invoke_from == InvokeFrom.WEB_APP,
                Conversation.is_pinned == is_pinned,
                ~Conversation.is_deleted,
            )
            .order_by(desc("created_at"))
            .all()
        )

        return conversations

    def get_web_app_conversation_messages_with_page(
        self,
        token: str,
        conversation_id: UUID,
        req: GetConversationMessagesWithPageReq,
        account: Account,
    ) -> tuple[list[Message], Paginator]:
        """获取当前 WebApp 用户在指定会话中的消息。"""
        _, _, conversation = self.get_owned_web_conversation(
            token=token,
            conversation_id=conversation_id,
            account=account,
        )

        paginator = Paginator(db=self.db, req=req)
        filters = []
        if req.created_at.data:
            filters.append(
                Message.created_at <= datetime.fromtimestamp(req.created_at.data)
            )

        messages = paginator.paginate(
            self.db.session.query(Message)
            .options(joinedload(Message.agent_thoughts))
            .filter(
                Message.conversation_id == conversation.id,
                Message.status.in_([MessageStatus.STOP, MessageStatus.NORMAL]),
                Message.answer != "",
                ~Message.is_deleted,
                *filters,
            )
            .order_by(desc("created_at"))
        )

        return messages, paginator

    def delete_web_app_conversation(
        self,
        token: str,
        conversation_id: UUID,
        account: Account,
    ) -> Conversation:
        """删除当前 WebApp 用户在指定会话中的会话。"""
        app, _, conversation = self.get_owned_web_conversation(
            token=token,
            conversation_id=conversation_id,
            account=account,
        )
        self.agent_cleanup_service.cleanup_conversation(
            account_id=app.account_id,
            app_id=app.id,
            conversation_id=conversation.id,
        )
        return self.update(conversation, is_deleted=True)

    def update_web_app_conversation(
        self,
        token: str,
        conversation_id: UUID,
        account: Account,
        **kwargs,
    ) -> Conversation:
        """更新当前 WebApp 用户在指定会话中的会话属性。"""
        _, _, conversation = self.get_owned_web_conversation(
            token=token,
            conversation_id=conversation_id,
            account=account,
        )
        return self.update(conversation, **kwargs)
