"""为所有调用入口装配生产级 DeepAgents 运行时。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from deepagents.middleware.filesystem import FilesystemPermission
from injector import inject
from sqlalchemy.orm import Session, sessionmaker

from config.config import Config
from internal.core.agent_v2.runtime.assembly import AgentContextAssembler
from internal.core.agent_v2.runtime.assembly import (
    build_filesystem_permissions as _build_filesystem_permissions,
)
from internal.core.agent_v2.runtime.models import (
    AgentRunContext,
    AgentRuntimeResources,
)
from internal.core.agent_v2.persistence.store import get_configured_store
from internal.core.agent_v2.persistence.checkpoint import (
    get_configured_checkpointer,
)
from pkg.sqlalchemy import SQLAlchemy
from .skill_service import SkillService


@inject
@dataclass
class AgentRuntimeService:
    """统一装配调试、WebApp、API 和微信运行时。"""

    db: SQLAlchemy
    skill_service: SkillService

    def prepare(
        self,
        *,
        account_id: UUID,
        app_id: UUID,
        conversation_id: UUID,
        created_by: UUID,
        end_user_id: UUID | None,
        skill_bindings: list[dict[str, Any]] | None = None,
        subagents: list[dict[str, Any]] | None = None,
    ) -> AgentRuntimeResources:
        """装配按作用域隔离且失败关闭的运行时资源。"""
        # 1.按应用绑定读取当前版本的启用 Skill。
        skills = self.skill_service.list_enabled_latest(
            account_id=account_id,
            app_id=app_id,
            bindings=skill_bindings or [],
        )
        # 2.为本次 Agent 运行创建独立数据库 Session。
        runtime_session = sessionmaker(
            bind=self.db.engine,
            expire_on_commit=False,
        )()
        try:
            # 3.把身份、Skill、SubAgent 和运行配置交给统一装配器。
            return self._assemble_resources(
                runtime_session=runtime_session,
                account_id=account_id,
                app_id=app_id,
                conversation_id=conversation_id,
                created_by=created_by,
                end_user_id=end_user_id,
                skills=skills,
                subagents=subagents,
            )
        except Exception:
            # 4.装配失败时关闭本次临时 Session，避免连接泄漏。
            runtime_session.close()
            raise

    def _assemble_resources(
        self,
        *,
        runtime_session: Session,
        account_id: UUID,
        app_id: UUID,
        conversation_id: UUID,
        created_by: UUID,
        end_user_id: UUID | None,
        skills: list[Any],
        subagents: list[dict[str, Any]] | None,
    ) -> AgentRuntimeResources:
        """把业务输入交给独立上下文装配器。"""
        # 1.冻结租户、应用、会话和用户身份，形成运行时上下文。
        run_context = AgentRunContext(
            account_id=account_id,
            app_id=app_id,
            conversation_id=conversation_id,
            created_by=created_by,
            end_user_id=end_user_id,
        )
        # 2.由核心装配器创建 Backend、Store、Checkpoint 和权限策略。
        return AgentContextAssembler(
            checkpointer_provider=get_configured_checkpointer,
            store_provider=get_configured_store,
        ).assemble(
            runtime_session=runtime_session,
            run_context=run_context,
            skills=skills,
            subagents=subagents,
            settings=Config(),
        )


def build_filesystem_permissions(
    *,
    sandbox_enabled: bool = False,
) -> list[FilesystemPermission]:
    """返回统一文件权限策略。"""
    # 1.按是否启用 Sandbox 选择核心层的权限策略。
    return _build_filesystem_permissions(sandbox_enabled=sandbox_enabled)

__all__ = [
    "AgentRuntimeResources",
    "AgentRuntimeService",
    "build_filesystem_permissions",
]
