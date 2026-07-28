"""Assemble the production DeepAgents runtime for every invocation path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from deepagents.backends import CompositeBackend
from deepagents.middleware.filesystem import FilesystemPermission
from injector import inject
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field
from sqlalchemy.orm import sessionmaker

from config.config import Config
from internal.core.agent_v2.deny_backend import DenyBackend
from internal.core.agent_v2.memory_profile_backend import (
    MemoryProfileBackend,
    MemoryProfileRenderer,
)
from internal.core.agent_v2.saver import get_configured_checkpointer
from internal.core.agent_v2.skill_backend import SkillCatalogBackend
from internal.core.agent_v2.subagent_specs import validate_platform_subagents
from internal.core.agent_v2.workspace_backend import MyDifyWorkspaceBackend
from pkg.sqlalchemy import SQLAlchemy
from .memory_fact_service import MemoryFactService
from .skill_service import SkillService


@dataclass
class AgentRuntimeBundle:
    checkpointer: Any
    workspace_backend: CompositeBackend
    permissions: list[FilesystemPermission]
    skill_sources: list[str]
    memory_sources: list[str]
    subagent_skill_sources: dict[str, list[str]]


class _ExtractedFact(BaseModel):
    content: str
    importance: float = Field(ge=0, le=1)
    fact_type: str = "general"


class _ExtractedFacts(BaseModel):
    facts: list[_ExtractedFact] = Field(default_factory=list)


@inject
@dataclass
class AgentRuntimeService:
    """Single assembly boundary shared by debugger/WebApp/API/WeChat."""

    db: SQLAlchemy
    skill_service: SkillService
    memory_fact_service: MemoryFactService

    def prepare(
        self,
        *,
        account_id: UUID,
        app_id: UUID,
        conversation_id: UUID,
        created_by: UUID,
        end_user_id: UUID | None,
        query: str,
        skill_bindings: list[dict[str, Any]] | None = None,
        subagents: list[dict[str, Any]] | dict[str, dict] | None = None,
    ) -> AgentRuntimeBundle:
        skills = self.skill_service.list_enabled_latest(
            account_id=account_id,
            app_id=app_id,
            bindings=skill_bindings or [],
        )
        runtime_session = sessionmaker(
            bind=self.db.engine,
            expire_on_commit=False,
        )()
        workspace = MyDifyWorkspaceBackend(
            session=runtime_session,
            account_id=account_id,
            app_id=app_id,
            conversation_id=conversation_id,
            created_by=created_by,
        )
        normalized_subagents = validate_platform_subagents(subagents)
        routes = {
            "/workspace/": workspace,
            "/artifacts/": workspace,
            "/conversation_history/": workspace,
        }
        skill_sources: list[str] = []
        memory_sources: list[str] = []
        subagent_skill_sources: dict[str, list[str]] = {}
        if skills:
            routes["/skills/"] = SkillCatalogBackend(skills)
            skill_sources = ["/skills/"]
        skill_map = {skill.name: skill for skill in skills}
        for config in normalized_subagents:
            requested = config["skills"]
            missing = sorted(set(requested) - set(skill_map))
            if missing:
                from internal.core.agent_v2.subagent_specs import SubAgentSpecError

                raise SubAgentSpecError(
                    f"subagent {config['name']!r} 引用了未启用的 Skills: "
                    f"{', '.join(missing)}"
                )
            if requested:
                prefix = f"/subagent_skills/{config['name']}/"
                routes[prefix] = SkillCatalogBackend(
                    [skill_map[name] for name in requested]
                )
                subagent_skill_sources[config["name"]] = [prefix]
        memory_facts = self.memory_fact_service.recall_top_k(
            account_id=account_id,
            app_id=app_id,
            end_user_id=end_user_id,
            keyword=query,
            k=Config().AGENT_MEMORY_RECALL_K,
        )
        if memory_facts:
            routes["/memories/"] = MemoryProfileBackend(
                MemoryProfileRenderer().render(memory_facts)
            )
            memory_sources = ["/memories/profile.md"]
        backend = CompositeBackend(
            default=DenyBackend(),
            routes=routes,
            artifacts_root="/",
        )
        return AgentRuntimeBundle(
            checkpointer=get_configured_checkpointer(),
            workspace_backend=backend,
            permissions=build_filesystem_permissions(),
            skill_sources=skill_sources,
            memory_sources=memory_sources,
            subagent_skill_sources=subagent_skill_sources,
        )

    def extract_and_store_facts(
        self,
        *,
        account_id: UUID,
        app_id: UUID,
        end_user_id: UUID | None,
        conversation_id: UUID,
        message_id: UUID,
        query: str,
        answer: str,
        llm: BaseChatModel,
    ) -> int:
        """Extract durable user facts after a successful agent run."""
        if (
            not Config().AGENT_AUTO_EXTRACT_MEMORY
            or not query.strip()
            or not answer.strip()
        ):
            return 0
        try:
            extractor = llm.with_structured_output(_ExtractedFacts)
            result = extractor.invoke(
                "从以下一轮对话中提取未来仍有价值、且明确由用户表达的稳定事实。"
                "不要提取助手猜测、一次性请求、敏感凭证或短期状态。"
                "importance 低于 0.6 的内容不要返回。\n\n"
                f"用户: {query}\n助手: {answer}"
            )
        except Exception:
            return 0
        extracted = (
            result.get("facts", [])
            if isinstance(result, dict)
            else getattr(result, "facts", [])
        )
        stored = 0
        for item in extracted:
            content = (
                item.get("content")
                if isinstance(item, dict)
                else item.content
            )
            importance = (
                item.get("importance")
                if isinstance(item, dict)
                else item.importance
            )
            fact_type = (
                item.get("fact_type", "general")
                if isinstance(item, dict)
                else item.fact_type
            )
            if not isinstance(content, str) or not isinstance(
                importance,
                (int, float),
            ):
                continue
            fact = self.memory_fact_service.create_auto_fact(
                account_id=account_id,
                app_id=app_id,
                end_user_id=end_user_id,
                content=content,
                importance=float(importance),
                source_conversation_id=conversation_id,
                source_message_id=message_id,
                fact_type=fact_type,
            )
            stored += int(fact is not None)
        return stored


def build_filesystem_permissions() -> list[FilesystemPermission]:
    """Build DeepAgents file-tool permissions with an explicit deny fallback."""
    return [
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/.system/**", "/.audit/**"],
            mode="deny",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=[
                "/skills/**",
                "/subagent_skills/**",
                "/memories/**",
                "/conversation_history/**",
            ],
            mode="deny",
        ),
        FilesystemPermission(
            operations=["read"],
            paths=[
                "/skills/**",
                "/subagent_skills/**",
                "/memories/**",
                "/workspace/**",
                "/artifacts/**",
                "/conversation_history/**",
            ],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/workspace/**", "/artifacts/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/**"],
            mode="deny",
        ),
    ]

__all__ = [
    "AgentRuntimeBundle",
    "AgentRuntimeService",
    "build_filesystem_permissions",
]
