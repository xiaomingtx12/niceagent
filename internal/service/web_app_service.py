from dataclasses import dataclass
from typing import Any
from uuid import UUID

from injector import inject
from sqlalchemy import and_, desc, or_
from sqlalchemy.dialects.postgresql import insert

from internal.entity.app_entity import AppStatus
from internal.entity.conversation_entity import InvokeFrom
from internal.exception import NotFoundException, ForbiddenException
from internal.model import App, Account, Conversation, EndUser
from internal.schema.agent_runtime_schema import (
    AgentConversationCleanupReq,
    AgentInvocationContext,
)
from pkg.sqlalchemy import SQLAlchemy
from .app_config_service import AppConfigService
from .agent_conversation_cleanup_service import AgentConversationCleanupService
from .base_service import BaseService
from .conversation_service import ConversationService


@inject
@dataclass
class WebAppService(BaseService):
    """处理 WebApp Token、终端用户和 Conversation 的业务归属。

    本服务为 Agent Run 准备合法的调用上下文，不负责 Agent 装配或执行。
    """

    db: SQLAlchemy
    app_config_service: AppConfigService
    agent_cleanup_service: AgentConversationCleanupService
    conversation_service: ConversationService

    def create_conversation(self, token: str, account: Account) -> Conversation:
        """创建 WebApp 会话并由数据库分配会话 ID。"""
        app = self.get_web_app(token)
        end_user = self.resolve_web_app_end_user(app=app, account=account)
        return self.create(
            Conversation,
            app_id=app.id,
            name="新的会话",
            invoke_from=InvokeFrom.WEB_APP,
            created_by=end_user.id,
            owner_account_id=account.id,
        )

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
        # 2.账号归属和旧数据兼容统一交给会话模块。
        conversation = self.conversation_service.get_owned_conversation(
            conversation_id=conversation_id,
            account=account,
            app_id=app.id,
            allowed_sources=(InvokeFrom.WEB_APP, InvokeFrom.CREATOR_TASK),
        )
        return app, end_user, conversation

    def get_invocation_context(
        self,
        *,
        token: str,
        conversation_id: UUID,
        account: Account,
    ) -> AgentInvocationContext:
        """把 WebApp token 和账号映射为已校验的 Agent 调用上下文。"""
        app, end_user, conversation = self.get_owned_web_conversation(
            token=token,
            conversation_id=conversation_id,
            account=account,
        )
        return AgentInvocationContext(
            account_id=account.id,
            app_id=app.id,
            conversation_id=conversation.id,
            created_by=end_user.id,
            end_user_id=end_user.id,
            invoke_from=InvokeFrom.WEB_APP,
        )

    def get_owned_web_conversation_by_app_id(
        self,
        *,
        app_id: UUID,
        conversation_id: UUID,
        account: Account,
    ) -> Conversation:
        """使用稳定应用归属校验 WebApp 会话，不依赖可能轮换的访问 token。"""
        return self.conversation_service.get_owned_conversation(
            conversation_id=conversation_id,
            account=account,
            app_id=app_id,
            allowed_sources=(InvokeFrom.WEB_APP, InvokeFrom.CREATOR_TASK),
        )

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
        skills = [
            {
                "name": item.get("name", ""),
                "version": item.get("version", ""),
                "enabled": True,
            }
            for item in app_config.get("skills", [])
            if isinstance(item, dict) and item.get("enabled", True) and item.get("name")
        ]

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
                "native_file_input": bool(model_config.get("native_file_input", False)),
                "text_to_speech": app_config.get("text_to_speech"),
                "speech_to_text": app_config.get("speech_to_text"),
                "long_term_memory": app_config.get("long_term_memory"),
                "skills": skills,
            },
        }

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
                or_(
                    Conversation.owner_account_id == account.id,
                    and_(
                        Conversation.owner_account_id.is_(None),
                        Conversation.created_by.in_([end_user.id, account.id]),
                    ),
                ),
                Conversation.invoke_from.in_(
                    [
                        InvokeFrom.WEB_APP,
                        InvokeFrom.CREATOR_TASK,
                    ]
                ),
                Conversation.is_pinned == is_pinned,
                ~Conversation.is_deleted,
            )
            .order_by(desc("created_at"))
            .all()
        )

        return conversations

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
            AgentConversationCleanupReq(
                account_id=account.id,
                app_id=app.id,
                conversation_id=conversation.id,
            )
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
