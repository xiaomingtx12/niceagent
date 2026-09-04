"""已发布 Agent 的内部 LangGraph SDK 适配服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from injector import inject

from config import Config
from internal.core.agent_v2.entity.subagent_config import build_published_graph_id
from internal.core.agent_v2.protocol.internal_auth import (
    InternalAgentProtocolClaims,
    parse_internal_agent_protocol_token,
)
from internal.entity import WORKBENCH_ACTIVE_STATUSES
from internal.entity.app_entity import AppStatus
from internal.entity.conversation_entity import InvokeFrom
from internal.exception import ForbiddenException, NotFoundException, ValidateErrorException
from internal.model import Account, AgentRun, App
from internal.schema.agent_workbench_schema import WorkbenchCommandReq
from internal.schema.internal_agent_protocol_schema import (
    InternalRunCreateReq,
    InternalRunLaunchResp,
    InternalRunResp,
    InternalThreadResp,
)
from pkg.sqlalchemy import SQLAlchemy

from .agent_workbench_run_service import AgentWorkbenchRunService
from .agent_workbench_service import AgentWorkbenchService
from .base_service import BaseService


@inject
@dataclass
class InternalAgentProtocolService(BaseService):
    """把内部 SDK 的 threads/runs 调用映射到既有 Workbench 生命周期。"""

    db: SQLAlchemy
    config: Config
    workbench_service: AgentWorkbenchService
    run_service: AgentWorkbenchRunService

    def authenticate(self, token: str | None) -> InternalAgentProtocolClaims:
        """解析内部调用头，并校验账号与两个应用仍在授权范围内。"""
        claims = parse_internal_agent_protocol_token(
            token or "",
            secret=self.config.AGENT_INTERNAL_PROTOCOL_SECRET,
        )
        self._get_authorized_account(claims)
        return claims

    def create_thread(
        self,
        claims: InternalAgentProtocolClaims,
        thread_id: UUID,
    ) -> InternalThreadResp:
        """返回 SDK 线程对象；真实业务会话在首次 Run 时延迟创建。"""
        self._get_authorized_account(claims)
        now = _iso_now()
        return InternalThreadResp(
            thread_id=thread_id,
            created_at=now,
            updated_at=now,
            metadata={"graph_id": build_published_graph_id(claims.target_app_id)},
            status="idle",
        )

    def create_run(
        self,
        *,
        claims: InternalAgentProtocolClaims,
        thread_id: UUID,
        request: InternalRunCreateReq,
    ) -> InternalRunLaunchResp:
        """校验 graph、可选中断前序 Run，并经 Workbench 入队后台任务。"""
        account = self._get_authorized_account(claims)
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
                account=account,
                thread_id=thread_id,
                target_app_id=claims.target_app_id,
            )

        command = WorkbenchCommandReq.from_mapping(
            {
                "method": "run.start",
                "params": {"input": run_input},
            }
        )
        started = self.workbench_service.start_run(
            invoke_from=InvokeFrom.CREATOR_TASK,
            access_value=str(claims.target_app_id),
            thread_id=thread_id,
            account=account,
            command=command,
        )
        if started.type == "error":
            return InternalRunLaunchResp(conflict_message=started.message)

        run_id = started.result.run_id
        row = self.run_service.get_run(run_id)
        try:
            return InternalRunLaunchResp(
                run=self._to_run_response(
                    row=row,
                    thread_id=thread_id,
                    target_app_id=claims.target_app_id,
                    multitask_strategy=request.multitask_strategy or "reject",
                )
            )
        finally:
            self.db.session.close()

    def get_thread(
        self,
        *,
        claims: InternalAgentProtocolClaims,
        thread_id: UUID,
    ) -> InternalThreadResp:
        """读取 SDK 所需的当前 State，用于异步任务获取最终结果。"""
        account = self._get_authorized_account(claims)
        state = self.workbench_service.get_state(
            invoke_from=InvokeFrom.CREATOR_TASK,
            access_value=str(claims.target_app_id),
            thread_id=thread_id,
            account=account,
        )
        now = _iso_now()
        active = state.metadata.status in WORKBENCH_ACTIVE_STATUSES
        return InternalThreadResp(
            thread_id=thread_id,
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
        thread_id: UUID,
        run_id: UUID,
    ) -> InternalRunResp:
        """读取指定内部 Run，禁止跨应用或跨线程访问。"""
        self._get_authorized_account(claims)
        row = self.run_service.get_run(run_id)
        try:
            return self._to_run_response(
                row=row,
                thread_id=thread_id,
                target_app_id=claims.target_app_id,
            )
        finally:
            self.db.session.close()

    def cancel_run(
        self,
        *,
        claims: InternalAgentProtocolClaims,
        thread_id: UUID,
        run_id: UUID,
    ) -> None:
        """将 SDK cancel 映射为 Workbench 指定 Run 取消。"""
        account = self._get_authorized_account(claims)
        try:
            self.workbench_service.cancel_run(
                invoke_from=InvokeFrom.CREATOR_TASK,
                access_value=str(claims.target_app_id),
                thread_id=thread_id,
                run_id=run_id,
                account=account,
            )
        finally:
            self.db.session.close()

    def _request_active_run_cancel(
        self,
        *,
        account: Account,
        thread_id: UUID,
        target_app_id: UUID,
    ) -> None:
        """为 update 语义请求停止前序运行，不等待其完成。"""
        active = self.run_service.get_latest_run(
            thread_id=thread_id,
            invoke_from=InvokeFrom.CREATOR_TASK,
        )
        if active is None or active.status not in WORKBENCH_ACTIVE_STATUSES:
            return
        if active.app_id != target_app_id:
            raise ForbiddenException("内部 Agent Protocol 线程不属于当前应用")
        self.workbench_service.cancel_run(
            invoke_from=InvokeFrom.CREATOR_TASK,
            access_value=str(target_app_id),
            thread_id=thread_id,
            run_id=active.id,
            account=account,
        )

    def _get_authorized_account(
        self,
        claims: InternalAgentProtocolClaims,
    ) -> Account:
        """复核签名声明对应的账号、父应用和目标发布应用。"""
        try:
            account = self.db.session.get(Account, claims.account_id)
            parent = self.db.session.query(App.id).filter(
                App.id == claims.parent_app_id,
                App.account_id == claims.account_id,
            ).one_or_none()
            target = self.db.session.query(App.id).filter(
                App.id == claims.target_app_id,
                App.account_id == claims.account_id,
                App.status == AppStatus.PUBLISHED,
            ).one_or_none()
            if account is None or parent is None or target is None:
                raise ForbiddenException("内部 Agent Protocol 调用不在有效应用范围内")
            return account
        finally:
            self.db.session.close()

    @staticmethod
    def _to_run_response(
        *,
        row: AgentRun | None,
        thread_id: UUID,
        target_app_id: UUID,
        multitask_strategy: str = "reject",
    ) -> InternalRunResp:
        """把平台 Run 映射为 LangGraph SDK 可识别的状态对象。"""
        if (
            row is None
            or row.thread_id != thread_id
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
            thread_id=thread_id,
            assistant_id=build_published_graph_id(target_app_id),
            status=status,
            multitask_strategy=multitask_strategy,
            metadata={},
            created_at=created_at,
            updated_at=updated_at,
            error=(str(row.error_message) if status == "error" and row.error_message else None),
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
