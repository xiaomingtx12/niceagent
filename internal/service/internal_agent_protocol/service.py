"""已发布 Agent 的内部 LangGraph SDK 适配服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from injector import inject
from sqlalchemy import and_, or_, select

from config import Config
from internal.entity.subagent_config import build_published_graph_id
from internal.service.internal_agent_protocol.auth import (
    InternalAgentProtocolClaims,
    parse_internal_agent_protocol_token,
)
from internal.entity import AGENT_EXECUTION_ACTIVE_STATUSES
from internal.entity.agent_execution_interaction import ExecutionRejected
from internal.entity.app_entity import AppStatus
from internal.entity.conversation_entity import InvokeFrom
from internal.exception import (
    ForbiddenException,
    NotFoundException,
    ValidateErrorException,
)
from internal.model import Account, AgentExecution, App, Conversation, EndUser
from internal.schema.agent_protocol.internal_agent_protocol_schema import (
    InternalRunCreateReq,
    InternalRunInputReq,
    InternalRunResp,
    InternalThreadResp,
)
from pkg.sqlalchemy import SQLAlchemy

from internal.service.agent.agent_execution_service import AgentExecutionService
from internal.service.conversation.conversation_service import ConversationService


@inject
@dataclass
class InternalAgentProtocolService:
    """把内部 SDK 的 threads/runs 调用映射到既有 Agent Run 生命周期。"""

    db: SQLAlchemy
    config: Config
    run_service: AgentExecutionService
    conversation_service: ConversationService

    def authenticate(self, token: str | None) -> InternalAgentProtocolClaims:
        """验证签名与有效期；每个业务入口随后只校验一次当前应用授权。"""
        return parse_internal_agent_protocol_token(
            token or "",
            secret=self.config.AGENT_INTERNAL_PROTOCOL_SECRET,
        )

    def create_conversation(
        self,
        claims: InternalAgentProtocolClaims,
    ) -> InternalThreadResp:
        """创建业务会话，并投影为 SDK thread 响应。"""
        account = self._get_authorized_account(claims)
        with self.db.auto_commit():
            conversation = Conversation(
                app_id=claims.target_app_id, name="新的会话",
                invoke_from=InvokeFrom.CREATOR_TASK,
                created_by=account.id, owner_account_id=account.id,
            )
            self.db.session.add(conversation)
            self.db.session.flush()
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
    ) -> InternalRunResp | ExecutionRejected:
        """校验 graph、可选中断前序 Run，并经 Agent Run 服务入队后台任务。"""
        # 1.验证签名声明、目标应用和 graph_id，再解析 SDK 输入。
        account = self._get_authorized_account(claims)
        conversation = self._get_owned_conversation(
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

        try:
            command_input = InternalRunInputReq.model_validate(
                run_input
            ).to_run_input()
        except ValueError as exc:
            raise ValidateErrorException(f"后台 Run 输入格式错误: {exc}") from exc
        if request.multitask_strategy == "interrupt":
            # 2.update 语义由 Agent Run 服务处理停止、等待和新 Run 创建。
            started = self.run_service.restart_run(
                account_id=account.id, app_id=conversation.app_id, conversation_id=conversation.id,
                created_by=account.id, invoke_from=InvokeFrom.CREATOR_TASK, run_input=command_input,
            )
        else:
            started = self.run_service.start_run(
                account_id=account.id, app_id=conversation.app_id, conversation_id=conversation.id,
                created_by=account.id, invoke_from=InvokeFrom.CREATOR_TASK,
                run_input=command_input, user_input_enabled=False,
            )
        if isinstance(started, ExecutionRejected):
            return started

        run_id = started.run_id
        # 3.只返回新 Run 摘要，不暴露执行内部细节。
        row = self.run_service.get_run(run_id)
        return self._to_run_response(
            row=row,
            multitask_strategy=request.multitask_strategy or "reject",
        )

    def get_conversation(
        self,
        *,
        claims: InternalAgentProtocolClaims,
        conversation_id: UUID,
    ) -> InternalThreadResp:
        """读取 SDK 所需的当前 State，用于异步任务获取最终结果。"""
        account = self._get_authorized_account(claims)
        conversation = self._get_owned_conversation(
            conversation_id=conversation_id, app_id=claims.target_app_id,
            account=account,
        )
        metadata = self.run_service.get_run_metadata(
            conversation_id=conversation_id, invoke_from=InvokeFrom.CREATOR_TASK,
        )
        rows, _, _ = self.conversation_service.get_turn_page(conversation_id=conversation_id, page_size=1)
        messages = []
        for message in rows:
            messages.append({"type": "human", "id": f"{message.id}:human", "content": message.query})
            if message.answer:
                messages.append({"type": "ai", "id": f"{message.id}:assistant", "content": message.answer})
        return InternalThreadResp(
            thread_id=conversation_id,
            created_at=_format_datetime(conversation.created_at),
            updated_at=_format_datetime(conversation.updated_at),
            metadata={"graph_id": build_published_graph_id(claims.target_app_id)},
            status="busy" if metadata.status in AGENT_EXECUTION_ACTIVE_STATUSES else "idle",
            values={"messages": messages},
        )

    def get_run(
        self,
        *,
        claims: InternalAgentProtocolClaims,
        conversation_id: UUID,
        run_id: UUID,
    ) -> InternalRunResp:
        """读取指定内部 Run，禁止跨应用或跨会话访问。"""
        account = self._get_authorized_account(claims)
        self._get_owned_conversation(
            app_id=claims.target_app_id, conversation_id=conversation_id, account=account,
        )
        row = (
            self.db.session.query(AgentExecution)
            .filter(
                AgentExecution.id == run_id,
                AgentExecution.app_id == claims.target_app_id,
                AgentExecution.conversation_id == conversation_id,
                AgentExecution.invoke_from == InvokeFrom.CREATOR_TASK.value,
            )
            .one_or_none()
        )
        if row is None:
            raise NotFoundException("Agent Run 不存在或不属于当前会话")
        return self._to_run_response(
            row=row,
        )

    def cancel_run(
        self,
        *,
        claims: InternalAgentProtocolClaims,
        conversation_id: UUID,
        run_id: UUID,
    ) -> None:
        """将 SDK cancel 映射为指定 Agent Run 取消。"""
        account = self._get_authorized_account(claims)
        conversation = self._get_owned_conversation(
            app_id=claims.target_app_id, conversation_id=conversation_id, account=account,
        )
        self.run_service.cancel_run(
            app_id=conversation.app_id, conversation_id=conversation.id,
            invoke_from=InvokeFrom.CREATOR_TASK, run_id=run_id,
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

    def _get_owned_conversation(
        self,
        *,
        app_id: UUID,
        conversation_id: UUID,
        account: Account,
    ) -> Conversation:
        """应用授权通过后校验内部 SDK 会话；兼容旧 WebApp 记录，但不借用任务中心规则。"""
        legacy_end_users = select(EndUser.id).where(
            EndUser.app_id == app_id,
            EndUser.account_id == account.id,
        )
        legacy_owner = or_(
            Conversation.created_by == account.id,
            and_(Conversation.invoke_from == InvokeFrom.WEB_APP, Conversation.created_by.in_(legacy_end_users)),
        )
        conversation = (
            self.db.session.query(Conversation)
            .filter(
                Conversation.id == conversation_id,
                Conversation.app_id == app_id,
                Conversation.invoke_from.in_((InvokeFrom.CREATOR_TASK, InvokeFrom.WEB_APP)),
                Conversation.is_deleted.is_(False),
                or_(
                    Conversation.owner_account_id == account.id,
                    and_(Conversation.owner_account_id.is_(None), legacy_owner),
                ),
            )
            .one_or_none()
        )
        if conversation is None:
            raise NotFoundException("内部 Agent Protocol 会话不存在或无权访问")
        return conversation

    @staticmethod
    def _to_run_response(
        *,
        row: AgentExecution | None,
        multitask_strategy: str = "reject",
    ) -> InternalRunResp:
        """把平台 Run 映射为 LangGraph SDK 可识别的状态对象。"""
        if row is None:
            raise NotFoundException("内部 Agent Protocol Run 不存在")
        # 等待填写表单的 Run 仍然存活，对 SDK 表现为运行中而不是错误。
        status = {
            "running": "running",
            "waiting_input": "running",
            "completed": "success",
            "failed": "error",
            "cancelled": "cancelled",
        }.get(str(row.status), "error")
        created_at = _format_datetime(row.created_at)
        # Execution 没有通用更新时间；SDK 时间取已保存的结束时间，运行中取创建时间。
        updated_at = _format_datetime(row.finished_at or row.created_at)
        return InternalRunResp(
            run_id=row.id,
            thread_id=row.conversation_id,
            assistant_id=build_published_graph_id(row.app_id),
            status=status,
            multitask_strategy=multitask_strategy,
            metadata={},
            created_at=created_at,
            updated_at=updated_at,
            error=row.error_message or ("Agent 运行失败" if status == "error" else None),
        )


def _format_datetime(value: datetime | None) -> str:
    """兼容数据库无时区时间字段并输出 UTC 字符串。"""
    if value is None:
        value = datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.isoformat().replace("+00:00", "Z")


def _extract_run_input(request: InternalRunCreateReq) -> dict[str, Any] | None:
    """把 SDK 的 input/command 统一投影为 Agent Run 输入。"""
    if request.input is not None:
        return request.input
    command = request.command
    if not isinstance(command, dict):
        return None
    update = command.get("update")
    return update if isinstance(update, dict) else None


__all__ = ["InternalAgentProtocolService"]
