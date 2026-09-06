"""Agent Protocol 工作台的应用服务门面。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from typing import Any
from uuid import UUID

from injector import inject

from internal.entity import WorkbenchRunInput, WorkbenchRunStatus
from internal.entity.conversation_entity import InvokeFrom
from internal.entity.app_entity import AppStatus
from internal.exception import ForbiddenException, NotFoundException
from internal.model import Account, App, EndUser
from internal.schema.agent_workbench_schema import (
    WorkbenchCancelResp,
    WorkbenchCommandErrorResp,
    WorkbenchCommandSuccessResp,
    WorkbenchMessagePageResp,
    WorkbenchRunObservabilityResp,
    WorkbenchStateResp,
    WorkbenchSubscriptionReq,
)
from pkg.sqlalchemy import SQLAlchemy

from .agent_workbench_access_service import AgentWorkbenchAccessService
from .agent_workbench_event_queue import AgentWorkbenchEventQueueManager
from .agent_workbench_run_coordinator import AgentWorkbenchRunCoordinator
from .agent_workbench_run_factory import AgentWorkbenchRunFactory
from .agent_workbench_run_service import AgentWorkbenchRunService
from .base_service import BaseService
from .conversation_service import ConversationService


@inject
@dataclass
class AgentWorkbenchService(BaseService):
    """对外提供鉴权后的 Workbench Run、状态、取消和事件订阅入口。"""

    db: SQLAlchemy
    access_service: AgentWorkbenchAccessService
    run_service: AgentWorkbenchRunService
    queue_manager: AgentWorkbenchEventQueueManager
    run_factory: AgentWorkbenchRunFactory
    run_coordinator: AgentWorkbenchRunCoordinator
    conversation_service: ConversationService

    # -------------------- Public Workbench API --------------------

    def subscribe(
        self,
        *,
        invoke_from: InvokeFrom,
        access_value: str,
        conversation_id: UUID,
        account: Account,
        subscription: WorkbenchSubscriptionReq,
    ) -> Iterator[dict[str, Any] | None]:
        """校验会话归属后创建事件订阅。"""
        # 1.校验当前入口是否拥有会话访问权限。
        self._authorize_conversation_access(
            invoke_from=invoke_from,
            access_value=access_value,
            conversation_id=conversation_id,
            account=account,
        )
        # 2.交给队列管理器处理本地队列、Redis 重放和数据库降级。
        return self.queue_manager.subscribe(
            invoke_from=invoke_from,
            conversation_id=conversation_id,
            subscription=subscription,
        )

    def get_state(
        self,
        *,
        invoke_from: InvokeFrom,
        access_value: str,
        conversation_id: UUID,
        account: Account,
    ) -> WorkbenchStateResp:
        """校验会话归属后读取工作台状态。"""
        # 1.校验当前入口是否拥有会话访问权限。
        self._authorize_conversation_access(
            invoke_from=invoke_from,
            access_value=access_value,
            conversation_id=conversation_id,
            account=account,
        )
        # 2.合并本地活动 Run 与数据库历史状态。
        return self.run_service.get_state(
            invoke_from=invoke_from,
            conversation_id=conversation_id,
            active_run=self.queue_manager.get(conversation_id),
        )

    def get_message_page(
        self,
        *,
        invoke_from: InvokeFrom,
        access_value: str,
        conversation_id: UUID,
        account: Account,
        before: UUID | None = None,
        page_size: int | None = None,
    ) -> WorkbenchMessagePageResp:
        """校验会话归属后读取一页业务消息。"""
        # 1.校验当前入口是否拥有会话访问权限。
        self._authorize_conversation_access(
            invoke_from=invoke_from,
            access_value=access_value,
            conversation_id=conversation_id,
            account=account,
        )
        # 2.按稳定游标读取业务消息页。
        return self.run_service.get_message_page(
            conversation_id=conversation_id,
            before=before,
            page_size=page_size,
        )

    def start_run(
        self,
        *,
        invoke_from: InvokeFrom,
        access_value: str,
        conversation_id: UUID,
        account: Account,
        run_input: WorkbenchRunInput,
        command_id: int | None = None,
    ) -> WorkbenchCommandSuccessResp | WorkbenchCommandErrorResp:
        """校验调用入口，并创建一条待执行的工作台 Run。"""
        try:
            # 1.先解析命令并校验入口，保留授权 ORM 对象供 Factory 使用。
            authorized = self.access_service.authorize_run_start(
                invoke_from=invoke_from,
                access_value=access_value,
                conversation_id=conversation_id,
                account=account,
            )

            # 2.协调去重、入库和后台执行，完成后再释放请求 Session。
            return self.run_coordinator.enqueue(
                conversation_id=conversation_id,
                invoke_from=invoke_from,
                input_message_id=run_input.input_message_id,
                command_id=command_id,
                run_builder=partial(
                    self.run_factory.create_run,
                    invoke_from=invoke_from,
                    account=account,
                    run_input=run_input,
                    authorized=authorized,
                ),
            )
        finally:
            self.db.session.close()

    def create_service_api_conversation(
        self,
        *,
        app_id: UUID,
        account: Account,
        end_user_id: UUID | None = None,
    ) -> UUID:
        """为开放 API 创建会话，ID 始终由后端生成。"""
        try:
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

            if end_user_id is None:
                end_user = EndUser(tenant_id=app.account_id, app_id=app.id)
                self.db.session.add(end_user)
                self.db.session.flush()
            else:
                end_user = self.db.session.get(EndUser, end_user_id)
                if (
                    end_user is None
                    or end_user.app_id != app.id
                    or end_user.tenant_id != app.account_id
                ):
                    raise ForbiddenException("终端用户不属于当前应用")

            with self.db.auto_commit():
                conversation = self.conversation_service.create_conversation(
                    app_id=app.id,
                    invoke_from=InvokeFrom.SERVICE_API,
                    created_by=end_user.id,
                    owner_account_id=account.id,
                )
            return conversation.id
        finally:
            self.db.session.close()

    def cancel_run(
        self,
        *,
        invoke_from: InvokeFrom,
        access_value: str,
        conversation_id: UUID,
        run_id: UUID,
        account: Account,
    ) -> WorkbenchCancelResp:
        """鉴权后请求停止指定 Agent Run。"""
        try:
            # 1.校验会话访问权限，再读取目标 Run。
            self._authorize_conversation_access(
                invoke_from=invoke_from,
                access_value=access_value,
                conversation_id=conversation_id,
                account=account,
            )
            row = self.run_service.get_run(run_id)
            if (
                row is None
                or row.conversation_id != conversation_id
                or row.invoke_from != invoke_from.value
            ):
                raise NotFoundException("Agent Run 不存在或不属于当前会话")
            # 2.再次按持久化 Run 的稳定归属校验权限。
            self.access_service.authorize_persisted_run(row, account)
            # 3.写入取消状态并通知本地/跨进程执行器。
            cancel_requested = self.run_coordinator.request_cancel(
                row,
                conversation_id=conversation_id,
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
        finally:
            # 取消是 SDK 高频轮询路径，不能把连接交给请求上下文兜底。
            self.db.session.close()

    def get_run_observability(
        self,
        *,
        run_id: UUID,
        account: Account,
    ) -> WorkbenchRunObservabilityResp:
        """鉴权后读取平台持久化的 Agent Run 观测链路。"""
        try:
            # 1.读取并校验目标 Run 的稳定归属。
            row = self.run_service.get_run(run_id)
            if row is None:
                raise NotFoundException("Agent Run 不存在")
            self.access_service.authorize_persisted_run(row, account)
            # 2.投影平台保存的观测快照，保持查询接口只读。
            return WorkbenchRunObservabilityResp(
                run_id=row.id,
                trace_id=row.id,
                status=str(row.status),
                started_at=row.started_at,
                finished_at=row.finished_at,
                observability=dict(row.observability or {}),
            )
        finally:
            self.db.session.close()

    def _authorize_conversation_access(
        self,
        *,
        invoke_from: InvokeFrom,
        access_value: str,
        conversation_id: UUID,
        account: Account,
    ) -> None:
        """统一工作台会话鉴权参数。"""
        self.access_service.authorize_conversation(
            invoke_from=invoke_from,
            access_value=access_value,
            conversation_id=conversation_id,
            account=account,
        )


__all__ = ["AgentWorkbenchService"]
