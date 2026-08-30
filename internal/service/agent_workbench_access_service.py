"""Agent 工作台访问边界服务。"""

from dataclasses import dataclass
from uuid import UUID

from injector import inject

from internal.entity.app_entity import AppStatus
from internal.entity.conversation_entity import InvokeFrom
from internal.exception import ForbiddenException, NotFoundException
from internal.model import Account, AgentRun, App, Conversation, EndUser
from pkg.sqlalchemy import SQLAlchemy

from .base_service import BaseService
from .web_app_service import WebAppService


@dataclass(frozen=True)
class AuthorizedWorkbenchContext:
    """一次 Run 启动已校验、可直接交给 Factory 复用的业务对象。"""

    app: App | None
    conversation: Conversation | None
    end_user: EndUser | None = None


@inject
@dataclass
class AgentWorkbenchAccessService(BaseService):
    """校验工作台各入口对线程、App 和持久化 Run 的访问权限。

    这是业务授权边界，只读取并核对归属关系，不创建 Run、不装配 Agent。
    """

    db: SQLAlchemy
    web_app_service: WebAppService

    def authorize_run_start(
        self,
        *,
        invoke_from: InvokeFrom,
        access_value: str,
        thread_id: UUID,
        account: Account,
    ) -> AuthorizedWorkbenchContext:
        """校验 Run 启动权限；WebApp 首次启动允许延迟创建线程。"""
        try:
            if invoke_from is InvokeFrom.DEBUGGER:
                row = (
                    self.db.session.query(App, Conversation)
                    .join(Conversation, Conversation.app_id == App.id)
                    .filter(
                        App.id == UUID(access_value),
                        App.account_id == account.id,
                        Conversation.id == thread_id,
                        Conversation.invoke_from == InvokeFrom.DEBUGGER,
                        Conversation.is_deleted.is_(False),
                    )
                    .one_or_none()
                )
                if row is None:
                    raise ForbiddenException("工作台线程不存在或无权访问")
                return AuthorizedWorkbenchContext(
                    app=row[0],
                    conversation=row[1],
                )
            if invoke_from is InvokeFrom.WEB_APP:
                app = self.web_app_service.get_web_app(access_value)
                end_user = self.web_app_service.resolve_web_app_end_user(
                    app=app,
                    account=account,
                )
                conversation = self.db.session.get(Conversation, thread_id)
                if conversation is not None and (
                    conversation.app_id != app.id
                    or conversation.invoke_from != InvokeFrom.WEB_APP
                    or conversation.created_by not in {end_user.id, account.id}
                    or conversation.is_deleted is True
                ):
                    raise ForbiddenException("会话不存在或不属于当前用户")
                return AuthorizedWorkbenchContext(
                    app=app,
                    conversation=conversation,
                    end_user=end_user,
                )
            if invoke_from is InvokeFrom.ASSISTANT_AGENT:
                conversation = account.assistant_agent_conversation
                if (
                    conversation.id != thread_id
                    or conversation.id != account.assistant_agent_conversation_id
                    or conversation.created_by != account.id
                ):
                    raise ForbiddenException("辅助 Agent 线程不存在或无权访问")
                return AuthorizedWorkbenchContext(
                    app=None,
                    conversation=conversation,
                )
            if invoke_from is InvokeFrom.SERVICE_API:
                row = (
                    self.db.session.query(App, Conversation)
                    .outerjoin(
                        Conversation,
                        Conversation.id == thread_id,
                    )
                    .filter(
                        App.id == UUID(access_value),
                        App.account_id == account.id,
                        App.status == AppStatus.PUBLISHED,
                    )
                    .one_or_none()
                )
                if row is None:
                    raise ForbiddenException("应用不存在、未发布或无权访问")
                app, conversation = row
                if conversation is not None and (
                    conversation.app_id != app.id
                    or conversation.invoke_from != InvokeFrom.SERVICE_API
                ):
                    raise ForbiddenException("服务 API 线程不属于当前应用")
                return AuthorizedWorkbenchContext(
                    app=app,
                    conversation=conversation,
                )
            raise NotFoundException("未知 Agent Protocol 调用来源")
        finally:
            self.db.session.close()

    def authorize_thread(
        self,
        *,
        invoke_from: InvokeFrom,
        access_value: str,
        thread_id: UUID,
        account: Account,
        allow_missing: bool = False,
    ) -> None:
        """校验线程归属，并在短查询完成后释放数据库连接。"""
        if invoke_from is InvokeFrom.DEBUGGER:
            authorized = (
                self.db.session.query(App.id)
                .join(Conversation, Conversation.app_id == App.id)
                .filter(
                    App.id == UUID(access_value),
                    App.account_id == account.id,
                    Conversation.id == thread_id,
                    Conversation.invoke_from == InvokeFrom.DEBUGGER,
                    Conversation.is_deleted.is_(False),
                )
                .one_or_none()
            )
            if authorized is None:
                raise ForbiddenException("工作台线程不存在或无权访问")
            self.db.session.close()
            return
        if invoke_from is InvokeFrom.WEB_APP:
            try:
                self.web_app_service.get_owned_web_conversation(
                    token=access_value,
                    conversation_id=thread_id,
                    account=account,
                )
            finally:
                self.db.session.close()
            return
        if invoke_from is InvokeFrom.ASSISTANT_AGENT:
            conversation = self.db.session.get(Conversation, thread_id)
            configured_app_id = UUID(str(account.assistant_agent_conversation.app_id))
            if (
                conversation is None
                or conversation.id != account.assistant_agent_conversation_id
                or conversation.app_id != configured_app_id
                or conversation.invoke_from != InvokeFrom.ASSISTANT_AGENT
                or conversation.created_by != account.id
            ):
                raise ForbiddenException("辅助 Agent 线程不存在或无权访问")
            self.db.session.close()
            return
        if invoke_from is InvokeFrom.SERVICE_API:
            query = (
                self.db.session.query(App.id)
                .join(Conversation, Conversation.app_id == App.id)
                .filter(
                    App.id == UUID(access_value),
                    App.account_id == account.id,
                    Conversation.id == thread_id,
                    Conversation.invoke_from == InvokeFrom.SERVICE_API,
                    Conversation.is_deleted.is_(False),
                )
            )
            if query.one_or_none() is None:
                if allow_missing:
                    app_exists = (
                        self.db.session.query(App.id)
                        .filter(
                            App.id == UUID(access_value),
                            App.account_id == account.id,
                        )
                        .one_or_none()
                    )
                    if app_exists is not None:
                        self.db.session.close()
                        return
                raise ForbiddenException("服务 API 线程不存在或无权访问")
            self.db.session.close()
            return
        raise NotFoundException("未知 Agent Protocol 调用来源")

    def authorize_persisted_run(self, run: AgentRun, account: Account) -> None:
        """使用稳定应用和会话归属校验取消权限。"""
        if run.invoke_from == InvokeFrom.DEBUGGER.value:
            app = (
                self.db.session.query(App)
                .filter(
                    App.id == run.app_id,
                    App.account_id == account.id,
                )
                .one_or_none()
            )
            conversation = self.db.session.get(Conversation, run.thread_id)
            if (
                app is None
                or conversation is None
                or conversation.app_id != run.app_id
                or conversation.invoke_from != InvokeFrom.DEBUGGER
            ):
                raise ForbiddenException("工作台线程不存在或无权访问")
            return
        if run.invoke_from == InvokeFrom.WEB_APP.value:
            self.web_app_service.get_owned_web_conversation_by_app_id(
                app_id=run.app_id,
                conversation_id=run.thread_id,
                account=account,
            )
            return
        if run.invoke_from == InvokeFrom.ASSISTANT_AGENT.value:
            conversation = self.db.session.get(Conversation, run.thread_id)
            if (
                conversation is None
                or conversation.id != account.assistant_agent_conversation_id
                or conversation.app_id != run.app_id
                or conversation.invoke_from != InvokeFrom.ASSISTANT_AGENT
                or conversation.created_by != account.id
            ):
                raise ForbiddenException("辅助 Agent Run 不存在或无权访问")
            return
        if run.invoke_from == InvokeFrom.SERVICE_API.value:
            app = (
                self.db.session.query(App)
                .filter(App.id == run.app_id, App.account_id == account.id)
                .one_or_none()
            )
            conversation = self.db.session.get(Conversation, run.thread_id)
            if (
                app is None
                or conversation is None
                or conversation.app_id != run.app_id
                or conversation.invoke_from != InvokeFrom.SERVICE_API
            ):
                raise ForbiddenException("Agent Protocol Run 不存在或无权访问")
            return
        raise ForbiddenException("工作台 Run 来源无效")


__all__ = ["AgentWorkbenchAccessService", "AuthorizedWorkbenchContext"]
