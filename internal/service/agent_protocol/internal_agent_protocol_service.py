"""已发布 Agent 的内部 LangGraph SDK 适配服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from injector import inject

from config import Config
from internal.entity.subagent_config import build_published_graph_id
from internal.service.agent_protocol.internal_auth import (
    InternalAgentProtocolClaims,
    parse_internal_agent_protocol_token,
)
from internal.entity import WORKBENCH_ACTIVE_STATUSES
from internal.entity.agent_run_interaction import RunRejected
from internal.service.agent.agent_workbench_query_service import AgentWorkbenchQueryService
from internal.entity.app_entity import AppStatus
from internal.entity.conversation_entity import InvokeFrom
from internal.exception import (
    ForbiddenException,
    NotFoundException,
    ValidateErrorException,
)
from internal.model import Account, AgentRun, App, Conversation
from internal.entity.agent_invocation import (
    AgentInvocationContext,
)
from internal.schema.agent.agent_workbench_schema import WorkbenchCommandInputReq
from internal.schema.agent_protocol.internal_agent_protocol_schema import (
    InternalRunCreateReq,
    InternalRunLaunchResp,
    InternalRunResp,
    InternalThreadResp,
)
from pkg.sqlalchemy import SQLAlchemy

from internal.service.agent.agent_workbench_run_service import AgentWorkbenchRunService
from internal.service.agent.agent_workbench_service import AgentWorkbenchService
from internal.service.common.base_service import BaseService
from internal.service.conversation.conversation_service import ConversationService
from internal.service.agent.creator_task_service import CreatorTaskService


@inject
@dataclass
class InternalAgentProtocolService(BaseService):
    """把内部 SDK 的 threads/runs 调用映射到既有 Workbench 生命周期。"""

    db: SQLAlchemy
    config: Config
    query_service: AgentWorkbenchQueryService
    workbench_service: AgentWorkbenchService
    run_service: AgentWorkbenchRunService
    conversation_service: ConversationService
    creator_task_service: CreatorTaskService

    def authenticate(self, token: str | None) -> InternalAgentProtocolClaims:
        """解析内部调用头，并校验账号与两个应用仍在授权范围内。"""
        claims = parse_internal_agent_protocol_token(
            token or "",
            secret=self.config.AGENT_INTERNAL_PROTOCOL_SECRET,
        )
        self._get_authorized_account(claims)
        return claims

    def create_conversation(
        self,
        claims: InternalAgentProtocolClaims,
    ) -> InternalThreadResp:
        """创建业务会话，并投影为 SDK thread 响应。"""
        account = self._get_authorized_account(claims)
        conversation = self.create(
            Conversation,
            app_id=claims.target_app_id,
            name="新的会话",
            invoke_from=InvokeFrom.CREATOR_TASK,
            created_by=account.id,
            owner_account_id=account.id,
        )
        now = _format_datetime(conversation.updated_at)
        return InternalThreadResp(
            thread_id=conversation.id,
            created_at=_format_datetime(conversation.created_at),
            updated_at=now,
            metadata={"graph_id": build_published_graph_id(claims.target_app_id)},
            status="idle",
        )

    def create_run(
        self,
        *,
        claims: InternalAgentProtocolClaims,
        conversation_id: UUID,
        request: InternalRunCreateReq,
    ) -> InternalRunLaunchResp:
        """校验 graph、可选中断前序 Run，并经 Workbench 入队后台任务。"""
        account = self._get_authorized_account(claims)
        context = self.creator_task_service.get_invocation_context(
            app_id=claims.target_app_id,
            conversation_id=conversation_id,
            account=account,
        )
        expected_graph_id = build_published_graph_id(claims.target_app_id)
        if request.assistant_id != expected_graph_id:
            raise ForbiddenException("内部 Agent Protocol graph_id 不在授权范围内")
        run_input = _extract_run_input(request)
        if not run_input:
            raise ValidateErrorException("后台 Run 缺少 input")

        # DeepAgents 的 update_async_task 使用 interrupt；复用 Workbench 的取消
        # 和顺序入队机制，避免同一 LangGraph thread 并发写 checkpoint。
        if request.multitask_strategy == "interrupt":
            self._request_active_run_cancel(
                context=context,
            )

        try:
            command_input = WorkbenchCommandInputReq.model_validate(
                run_input
            ).to_run_input()
        except ValueError as exc:
            raise ValidateErrorException(f"后台 Run 输入格式错误: {exc}") from exc
        started = self.workbench_service.start_run(
            context=context,
            run_input=command_input,
            user_input_enabled=False,
        )
        if isinstance(started, RunRejected):
            return InternalRunLaunchResp(conflict_message=started.message)

        run_id = started.run_id
        row = self.run_service.get_run(run_id)
        return InternalRunLaunchResp(
            run=self._to_run_response(
                row=row,
                conversation_id=conversation_id,
                target_app_id=claims.target_app_id,
                multitask_strategy=request.multitask_strategy or "reject",
            )
        )

    def get_conversation(
        self,
        *,
        claims: InternalAgentProtocolClaims,
        conversation_id: UUID,
    ) -> InternalThreadResp:
        """读取 SDK 所需的当前 State，用于异步任务获取最终结果。"""
        account = self._get_authorized_account(claims)
        state = self.query_service.get_state(
            context=self.creator_task_service.get_invocation_context(
                app_id=claims.target_app_id,
                conversation_id=conversation_id,
                account=account,
            ),
        )
        now = _iso_now()
        active = state.metadata.status in WORKBENCH_ACTIVE_STATUSES
        return InternalThreadResp(
            thread_id=conversation_id,
            created_at=now,
            updated_at=now,
            metadata={"graph_id": build_published_graph_id(claims.target_app_id)},
            status="busy" if active else "idle",
            values=state.values,
        )

    def get_run(
        self,
        *,
        claims: InternalAgentProtocolClaims,
        conversation_id: UUID,
        run_id: UUID,
    ) -> InternalRunResp:
        """读取指定内部 Run，禁止跨应用或跨会话访问。"""
        self._get_authorized_account(claims)
        row = self.run_service.get_run(run_id, recover=False)
        if (
            row is None
            or row.conversation_id != conversation_id
            or row.app_id != claims.target_app_id
            or row.invoke_from != InvokeFrom.CREATOR_TASK.value
        ):
            raise NotFoundException("内部 Agent Protocol Run 不存在")
        # 失联恢复会提交状态和事件，必须在目标 Run 的授权范围确认后执行。
        row = self.run_service.get_run(run_id, recover=True)
        return self._to_run_response(
            row=row,
            conversation_id=conversation_id,
            target_app_id=claims.target_app_id,
        )

    def cancel_run(
        self,
        *,
        claims: InternalAgentProtocolClaims,
        conversation_id: UUID,
        run_id: UUID,
    ) -> None:
        """将 SDK cancel 映射为 Workbench 指定 Run 取消。"""
        account = self._get_authorized_account(claims)
        self.workbench_service.cancel_run(
            context=self.creator_task_service.get_invocation_context(
                app_id=claims.target_app_id,
                conversation_id=conversation_id,
                account=account,
            ),
            run_id=run_id,
        )

    def _request_active_run_cancel(
        self,
        *,
        context: AgentInvocationContext,
    ) -> None:
        """为 update 语义请求停止前序运行，不等待其完成。"""
        active = self.run_service.get_latest_run(
            conversation_id=context.conversation_id,
            invoke_from=InvokeFrom.CREATOR_TASK,
        )
        if active is None or active.status not in WORKBENCH_ACTIVE_STATUSES:
            return
        if active.app_id != context.app_id:
            raise ForbiddenException("内部 Agent Protocol 会话不属于当前应用")
        self.workbench_service.cancel_run(
            context=context,
            run_id=active.id,
        )

    def _get_authorized_account(
        self,
        claims: InternalAgentProtocolClaims,
    ) -> Account:
        """复核签名声明对应的账号、父应用和目标发布应用。"""
        account = self.db.session.get(Account, claims.account_id)
        parent = (
            self.db.session.query(App.id)
            .filter(
                App.id == claims.parent_app_id,
                App.account_id == claims.account_id,
            )
            .one_or_none()
        )
        target = (
            self.db.session.query(App.id)
            .filter(
                App.id == claims.target_app_id,
                App.account_id == claims.account_id,
                App.status == AppStatus.PUBLISHED,
            )
            .one_or_none()
        )
        if account is None or parent is None or target is None:
            raise ForbiddenException("内部 Agent Protocol 调用不在有效应用范围内")
        return account

    @staticmethod
    def _to_run_response(
        *,
        row: AgentRun | None,
        conversation_id: UUID,
        target_app_id: UUID,
        multitask_strategy: str = "reject",
    ) -> InternalRunResp:
        """把平台 Run 映射为 LangGraph SDK 可识别的状态对象。"""
        if (
            row is None
            or row.conversation_id != conversation_id
            or row.app_id != target_app_id
            or row.invoke_from != InvokeFrom.CREATOR_TASK.value
        ):
            raise NotFoundException("内部 Agent Protocol Run 不存在")
        status = {
            "queued": "pending",
            "running": "running",
            "cancelling": "running",
            "completed": "success",
            "failed": "error",
            "cancelled": "cancelled",
        }.get(str(row.status), "error")
        created_at = _format_datetime(row.created_at)
        updated_at = _format_datetime(row.updated_at or row.created_at)
        return InternalRunResp(
            run_id=row.id,
            thread_id=conversation_id,
            assistant_id=build_published_graph_id(target_app_id),
            status=status,
            multitask_strategy=multitask_strategy,
            metadata={},
            created_at=created_at,
            updated_at=updated_at,
            error=(
                str(row.error_message)
                if status == "error" and row.error_message
                else None
            ),
        )


def _iso_now() -> str:
    """返回 SDK 使用的 UTC 时间戳。"""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _format_datetime(value: datetime | None) -> str:
    """兼容数据库无时区时间字段并输出 UTC 字符串。"""
    if value is None:
        return _iso_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.isoformat().replace("+00:00", "Z")


def _extract_run_input(request: InternalRunCreateReq) -> dict[str, Any] | None:
    """把 SDK 的 input/command 统一投影为 Workbench input。"""
    if request.input is not None:
        return request.input
    command = request.command
    if not isinstance(command, dict):
        return None
    update = command.get("update")
    return update if isinstance(update, dict) else None


__all__ = ["InternalAgentProtocolService"]
