"""Agent 工作台访问边界服务。"""

from dataclasses import dataclass
from uuid import UUID

from injector import inject

from config import Config
from internal.entity import AuthorizedWorkbenchContext
from internal.entity.app_entity import AppStatus
from internal.entity.conversation_entity import InvokeFrom
from internal.exception import ForbiddenException, NotFoundException
from internal.model import Account, AgentRun, App, Conversation, EndUser
from pkg.sqlalchemy import SQLAlchemy

from .base_service import BaseService
from .web_app_service import WebAppService


@inject
@dataclass
class AgentWorkbenchAccessService(BaseService):
    """校验工作台各入口对会话、App 和持久化 Run 的访问权限。

    这是业务授权边界，只读取并核对归属关系，不创建 Run、不装配 Agent。
    """

    db: SQLAlchemy
    web_app_service: WebAppService
    config: Config

    @staticmethod
    def _app_id(value: str) -> UUID:
        """Parse route supplied app ids in one place."""
        try:
            return UUID(value)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ForbiddenException("应用不存在或无权访问") from exc

    def _owned_published_app(self, app_id: UUID, account: Account) -> App:
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
            raise ForbiddenException("应用不存在、未发布或无权访问")
        return app

    # -------------------- Public authorization API --------------------

    def authorize_run_start(
        self,
        *,
        invoke_from: InvokeFrom,
        access_value: str,
        conversation_id: UUID,
        account: Account,
    ) -> AuthorizedWorkbenchContext:
        """校验 Run 启动权限，并保留授权 ORM 对象供同一事务的 Factory 复用。"""
        try:
            # 1.按调用来源校验 App、Conversation 和终端用户归属。
            if invoke_from is InvokeFrom.DEBUGGER:
                row = (
                    self.db.session.query(App, Conversation)
                    .join(Conversation, Conversation.app_id == App.id)
                    .filter(
                        App.id == self._app_id(access_value),
                        App.account_id == account.id,
                        Conversation.id == conversation_id,
                        Conversation.invoke_from == InvokeFrom.DEBUGGER,
                        Conversation.created_by == account.id,
                        Conversation.is_deleted.is_(False),
                    )
                    .one_or_none()
                )
                if row is None:
                    raise ForbiddenException("工作台会话不存在或无权访问")
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
                conversation = self.db.session.get(Conversation, conversation_id)
                if conversation is None or (
                    conversation.app_id != app.id
                    or conversation.invoke_from
                    not in {InvokeFrom.WEB_APP, InvokeFrom.CREATOR_TASK}
                    or not (
                        conversation.owner_account_id == account.id
                        or conversation.created_by in {end_user.id, account.id}
                    )
                    or conversation.is_deleted is True
                ):
                    raise ForbiddenException("会话不存在或不属于当前用户")
                return AuthorizedWorkbenchContext(
                    app=app,
                    conversation=conversation,
                    end_user=end_user,
                )
            if invoke_from is InvokeFrom.CREATOR_TASK:
                app = (
                    self._owned_published_app(
                        self._app_id(access_value), account
                    )
                )
                conversation = self.db.session.get(Conversation, conversation_id)
                if conversation is None:
                    raise ForbiddenException("会话不存在或不属于当前账号")
                end_user = (
                    self.db.session.query(EndUser)
                    .filter(
                        EndUser.app_id == app.id,
                        EndUser.external_user_id == account.id,
                    )
                    .one_or_none()
                )
                owner_matches = conversation.owner_account_id == account.id
                legacy_creator = conversation.created_by == account.id
                legacy_web_user = bool(
                    end_user is not None
                    and conversation.invoke_from == InvokeFrom.WEB_APP
                    and conversation.created_by == end_user.id
                )
                if (
                    conversation.app_id != app.id
                    or conversation.invoke_from
                    not in {InvokeFrom.CREATOR_TASK, InvokeFrom.WEB_APP}
                    or not (owner_matches or legacy_creator or legacy_web_user)
                    or conversation.is_deleted is True
                ):
                    raise ForbiddenException("会话不存在或不属于当前账号")
                return AuthorizedWorkbenchContext(app=app, conversation=conversation)
            if invoke_from is InvokeFrom.ASSISTANT_AGENT:
                conversation = self._assistant_conversation(
                    account=account,
                    conversation_id=conversation_id,
                )
                if conversation is None:
                    raise ForbiddenException("辅助 Agent 会话不存在或无权访问")
                return AuthorizedWorkbenchContext(
                    app=None,
                    conversation=conversation,
                )
            if invoke_from is InvokeFrom.SERVICE_API:
                row = (
                    self.db.session.query(App, Conversation)
                    .outerjoin(
                        Conversation,
                        Conversation.id == conversation_id,
                    )
                    .filter(
                        App.id == self._app_id(access_value),
                        App.account_id == account.id,
                        App.status == AppStatus.PUBLISHED,
                    )
                    .one_or_none()
                )
                if row is None:
                    raise ForbiddenException("应用不存在、未发布或无权访问")
                app, conversation = row
                if conversation is None or (
                    conversation.app_id != app.id
                    or conversation.invoke_from != InvokeFrom.SERVICE_API
                ):
                    raise ForbiddenException("服务 API 会话不属于当前应用")
                return AuthorizedWorkbenchContext(
                    app=app,
                    conversation=conversation,
                )
            raise NotFoundException("未知 Agent Protocol 调用来源")
        except Exception:
            # 成功结果会交给门面继续创建 Run；只有失败路径在此释放 Session。
            self.db.session.close()
            raise

    def authorize_conversation(
        self,
        *,
        invoke_from: InvokeFrom,
        access_value: str,
        conversation_id: UUID,
        account: Account,
    ) -> None:
        """校验会话归属，并保证所有鉴权分支都释放短查询 Session。"""
        try:
            # 1.委托来源专属校验实现，避免门面重复维护权限分支。
            self._authorize_conversation(
                invoke_from=invoke_from,
                access_value=access_value,
                conversation_id=conversation_id,
                account=account,
            )
        finally:
            # 2.统一兜底，覆盖鉴权异常和各入口提前返回的路径。
            self.db.session.close()

    def authorize_persisted_run(self, run: AgentRun, account: Account) -> None:
        """使用稳定应用和会话归属校验取消权限。"""
        # 1.使用 Run 中保存的 app_id、conversation_id 和 invoke_from 重新鉴权。
        # 2.本方法只做二次授权；Session 生命周期由上层取消/观测流程统一管理。
        if run.invoke_from == InvokeFrom.DEBUGGER.value:
            app = (
                self.db.session.query(App)
                .filter(
                    App.id == run.app_id,
                    App.account_id == account.id,
                )
                .one_or_none()
            )
            conversation = self.db.session.get(Conversation, run.conversation_id)
            if (
                app is None
                or conversation is None
                or conversation.app_id != run.app_id
                or conversation.invoke_from != InvokeFrom.DEBUGGER
                or conversation.created_by != account.id
                or conversation.is_deleted
            ):
                raise ForbiddenException("工作台会话不存在或无权访问")
            return
        if run.invoke_from == InvokeFrom.WEB_APP.value:
            self.web_app_service.get_owned_web_conversation_by_app_id(
                app_id=run.app_id,
                conversation_id=run.conversation_id,
                account=account,
            )
            return
        if run.invoke_from == InvokeFrom.CREATOR_TASK.value:
            app = self._owned_published_app(run.app_id, account)
            conversation = self.db.session.get(Conversation, run.conversation_id)
            if (
                conversation is None
                or conversation.app_id != run.app_id
                or conversation.is_deleted
                or not (
                    conversation.owner_account_id == account.id
                    or conversation.created_by == account.id
                )
            ):
                raise ForbiddenException("Creator 任务 Run 不存在或无权访问")
            return
        if run.invoke_from == InvokeFrom.ASSISTANT_AGENT.value:
            conversation = self._assistant_conversation(
                account=account,
                conversation_id=run.conversation_id,
            )
            if (
                conversation is None
                or conversation.app_id != run.app_id
            ):
                raise ForbiddenException("辅助 Agent Run 不存在或无权访问")
            return
        if run.invoke_from == InvokeFrom.SERVICE_API.value:
            app = (
                self.db.session.query(App)
                .filter(App.id == run.app_id, App.account_id == account.id)
                .one_or_none()
            )
            conversation = self.db.session.get(Conversation, run.conversation_id)
            if (
                app is None
                or conversation is None
                or conversation.app_id != run.app_id
                or conversation.invoke_from != InvokeFrom.SERVICE_API
            ):
                raise ForbiddenException("Agent Protocol Run 不存在或无权访问")
            return
        raise ForbiddenException("工作台 Run 来源无效")

    # -------------------- Internal authorization helper --------------------

    def _assistant_conversation(
        self,
        *,
        account: Account,
        conversation_id: UUID,
    ) -> Conversation | None:
        """按会话自身归属校验辅助 Agent 会话，不在鉴权路径创建数据。"""
        try:
            app_id = UUID(str(self.config.ASSISTANT_AGENT_ID))
        except (AttributeError, TypeError, ValueError):
            return None
        return (
            self.db.session.query(Conversation)
            .filter(
                Conversation.id == conversation_id,
                Conversation.app_id == app_id,
                Conversation.invoke_from == InvokeFrom.ASSISTANT_AGENT,
                Conversation.created_by == account.id,
                Conversation.is_deleted.is_(False),
            )
            .one_or_none()
        )

    def _authorize_conversation(
        self,
        *,
        invoke_from: InvokeFrom,
        access_value: str,
        conversation_id: UUID,
        account: Account,
    ) -> None:
        """执行各调用来源的会话归属校验。"""
        # 1.根据入口校验会话、应用和创建者的组合归属。
        if invoke_from is InvokeFrom.DEBUGGER:
            authorized = (
                self.db.session.query(App.id)
                .join(Conversation, Conversation.app_id == App.id)
                .filter(
                    App.id == self._app_id(access_value),
                    App.account_id == account.id,
                    Conversation.id == conversation_id,
                    Conversation.invoke_from == InvokeFrom.DEBUGGER,
                    Conversation.created_by == account.id,
                    Conversation.is_deleted.is_(False),
                )
                .one_or_none()
            )
            if authorized is None:
                raise ForbiddenException("工作台会话不存在或无权访问")
            return
        if invoke_from is InvokeFrom.WEB_APP:
            self.web_app_service.get_owned_web_conversation(
                token=access_value,
                conversation_id=conversation_id,
                account=account,
            )
            return
        if invoke_from is InvokeFrom.CREATOR_TASK:
            app = self._owned_published_app(self._app_id(access_value), account)
            conversation = self.db.session.get(Conversation, conversation_id)
            if conversation is None:
                raise ForbiddenException("会话不存在或不属于当前账号")
            end_user = (
                self.db.session.query(EndUser)
                .filter(
                    EndUser.app_id == app.id,
                    EndUser.external_user_id == account.id,
                )
                .one_or_none()
            )
            if (
                app is None
                or conversation is None
                or conversation.app_id != app.id
                or conversation.invoke_from
                not in {InvokeFrom.CREATOR_TASK, InvokeFrom.WEB_APP}
                or conversation.is_deleted is True
                or not (
                    conversation.owner_account_id == account.id
                    or conversation.created_by == account.id
                    or (
                        end_user is not None
                        and conversation.invoke_from == InvokeFrom.WEB_APP
                        and conversation.created_by == end_user.id
                    )
                )
            ):
                raise ForbiddenException("会话不存在或不属于当前账号")
            return
        if invoke_from is InvokeFrom.ASSISTANT_AGENT:
            conversation = self._assistant_conversation(
                account=account,
                conversation_id=conversation_id,
            )
            if conversation is None:
                raise ForbiddenException("辅助 Agent 会话不存在或无权访问")
            return
        if invoke_from is InvokeFrom.SERVICE_API:
            query = (
                self.db.session.query(App.id)
                .join(Conversation, Conversation.app_id == App.id)
                .filter(
                    App.id == self._app_id(access_value),
                    App.account_id == account.id,
                    Conversation.id == conversation_id,
                    Conversation.invoke_from == InvokeFrom.SERVICE_API,
                    Conversation.is_deleted.is_(False),
                )
            )
            if query.one_or_none() is None:
                raise ForbiddenException("服务 API 会话不存在或无权访问")
            return
        raise NotFoundException("未知 Agent Protocol 调用来源")


__all__ = ["AgentWorkbenchAccessService"]
