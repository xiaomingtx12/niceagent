"""Agent 工作台访问边界服务。"""

from dataclasses import dataclass
from uuid import UUID

from injector import inject

from internal.entity.conversation_entity import InvokeFrom
from internal.exception import ForbiddenException, NotFoundException
from internal.model import Account, AgentRun, App, Conversation
from pkg.sqlalchemy import SQLAlchemy

from .base_service import BaseService
from .web_app_service import WebAppService


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
    ) -> None:
        """校验 Run 启动权限；WebApp 首次启动允许延迟创建线程。"""
        if invoke_from is InvokeFrom.DEBUGGER:
            self.authorize_thread(
                invoke_from=invoke_from,
                access_value=access_value,
                thread_id=thread_id,
                account=account,
            )
            return
        if invoke_from in {InvokeFrom.ASSISTANT_AGENT, InvokeFrom.SERVICE_API}:
            self.authorize_thread(
                invoke_from=invoke_from,
                access_value=access_value,
                thread_id=thread_id,
                account=account,
                allow_missing=invoke_from is InvokeFrom.SERVICE_API,
            )
            return
        if invoke_from is not InvokeFrom.WEB_APP:
            raise NotFoundException("未知 Agent Protocol 调用来源")

        existing = self.get(Conversation, thread_id)
        if existing is None:
            app = self.web_app_service.get_web_app(access_value)
            self.web_app_service.resolve_web_app_end_user(
                app=app,
                account=account,
            )
            self.db.session.close()
            return
        self.web_app_service.get_owned_web_conversation(
            token=access_value,
            conversation_id=thread_id,
            account=account,
        )

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
            app = (
                self.db.session.query(App)
                .filter(
                    App.id == UUID(access_value),
                    App.account_id == account.id,
                )
                .one_or_none()
            )
            conversation = self.get(Conversation, thread_id)
            if (
                app is None
                or conversation is None
                or conversation.app_id != app.id
                or conversation.invoke_from != InvokeFrom.DEBUGGER
            ):
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
            app = (
                self.db.session.query(App)
                .filter(App.id == UUID(access_value), App.account_id == account.id)
                .one_or_none()
            )
            conversation = self.db.session.get(Conversation, thread_id)
            if app is None or (
                conversation is not None
                and (
                    conversation.app_id != app.id
                    or conversation.invoke_from != InvokeFrom.SERVICE_API
                )
            ):
                raise ForbiddenException("服务 API 线程不存在或无权访问")
            if conversation is None and not allow_missing:
                raise NotFoundException("服务 API 线程不存在")
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


__all__ = ["AgentWorkbenchAccessService"]
