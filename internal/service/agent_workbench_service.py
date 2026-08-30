"""Agent Protocol 工作台的应用服务门面。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from injector import inject

from internal.entity import WorkbenchRun, WorkbenchRunStatus
from internal.entity.conversation_entity import InvokeFrom
from internal.exception import NotFoundException
from internal.model import Account
from internal.schema.agent_runtime_schema import AgentInvocation
from internal.schema.agent_workbench_schema import (
    WorkbenchCancelResp,
    WorkbenchCommandErrorResp,
    WorkbenchCommandReq,
    WorkbenchCommandSuccessResp,
    WorkbenchMessagePageResp,
    WorkbenchStateResp,
    WorkbenchSubscriptionReq,
)
from pkg.sqlalchemy import SQLAlchemy

from .agent_workbench_access_service import AgentWorkbenchAccessService
from .agent_workbench_event_queue import AgentWorkbenchEventQueue
from .agent_workbench_run_coordinator import AgentWorkbenchRunCoordinator
from .agent_workbench_run_factory import AgentWorkbenchRunFactory
from .agent_workbench_run_service import AgentWorkbenchRunService
from .agent_workbench_stream_service import AgentWorkbenchStreamService
from .base_service import BaseService


@inject
@dataclass
class AgentWorkbenchService(BaseService):
    """对外提供鉴权后的 Workbench Run、状态、取消和事件订阅入口。"""

    db: SQLAlchemy
    access_service: AgentWorkbenchAccessService
    run_service: AgentWorkbenchRunService
    event_queue: AgentWorkbenchEventQueue
    run_factory: AgentWorkbenchRunFactory
    run_coordinator: AgentWorkbenchRunCoordinator
    stream_service: AgentWorkbenchStreamService

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
        return self.stream_service.subscribe(
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
        return self.run_service.get_state(
            invoke_from=invoke_from,
            thread_id=thread_id,
            active_run=self.event_queue.get(thread_id),
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
        run_input = command.to_run_input()
        authorized = self.access_service.authorize_run_start(
            invoke_from=invoke_from,
            access_value=access_value,
            thread_id=thread_id,
            account=account,
        )

        def create_run(run_id: UUID) -> WorkbenchRun:
            return self.run_factory.create_run(
                invoke_from=invoke_from,
                access_value=access_value,
                thread_id=thread_id,
                account=account,
                run_input=run_input,
                run_id=run_id,
                authorized=authorized,
            )

        return self.run_coordinator.enqueue(
            thread_id=thread_id,
            invoke_from=invoke_from,
            input_message_id=run_input.input_message_id,
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
        return self.run_coordinator.enqueue(
            thread_id=thread_id,
            invoke_from=invocation.invoke_from,
            input_message_id=invocation.input_message_id,
            command_id=command_id,
            create_run=lambda run_id: self.run_factory.create_from_invocation(
                invocation=invocation,
                thread_id=thread_id,
                run_id=run_id,
            ),
        )

    def cancel_run(
        self,
        *,
        invoke_from: InvokeFrom,
        access_value: str,
        thread_id: UUID,
        run_id: UUID,
        account: Account,
    ) -> WorkbenchCancelResp:
        """鉴权后请求停止指定 Agent Run。"""
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
        self.access_service.authorize_persisted_run(row, account)
        cancel_requested = self.run_coordinator.request_cancel(
            row,
            thread_id=thread_id,
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


__all__ = ["AgentWorkbenchService"]
