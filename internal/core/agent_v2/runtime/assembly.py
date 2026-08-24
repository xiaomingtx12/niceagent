"""Agent V2 的请求级能力装配。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from deepagents.backends.protocol import BackendProtocol, SandboxBackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from injector import inject
from sqlalchemy.orm import Session

from config.config import Config
from internal.core.agent_v2.backends.composite import build_composite_backend
from internal.core.agent_v2.backends.sandbox import (
    build_sandbox_backend,
    build_stable_sandbox_name,
)
from internal.core.agent_v2.backends.skill_catalog import SkillCatalogBackend
from internal.core.agent_v2.backends.workspace import (
    MyDifyWorkspaceBackend,
    WorkspaceRouteBackend,
)
from internal.core.agent_v2.backends.workspace_quota import WorkspaceLimits
from internal.core.agent_v2.runtime.memory import MemoryContextAssembler, MemoryProjection
from internal.core.agent_v2.runtime.models import (
    AgentRunContext,
    AgentRuntimeResources,
)
from internal.core.agent_v2.runtime.subagents import SubAgentSpecError
from internal.core.agent_v2.runtime.telemetry import (
    AgentTelemetry,
    LoggingAgentTelemetrySink,
)


@dataclass
class WorkspaceProjection:
    """会话文件、产物和历史外置路径。"""

    backend: MyDifyWorkspaceBackend
    routes: dict[str, BackendProtocol]


class WorkspaceContextAssembler:
    """把业务 Workspace 适配为 DeepAgents 路由。"""

    def build(
        self,
        *,
        runtime_session: Session,
        run_context: AgentRunContext,
        settings: Any,
        telemetry: AgentTelemetry,
    ) -> WorkspaceProjection:
        """创建本次运行独立的 Workspace Backend。"""
        workspace = MyDifyWorkspaceBackend(
            session=runtime_session,
            account_id=run_context.account_id,
            app_id=run_context.app_id,
            conversation_id=run_context.conversation_id,
            created_by=run_context.created_by,
            limits=WorkspaceLimits(
                max_bytes=settings.WORKSPACE_MAX_BYTES,
                max_files=settings.WORKSPACE_MAX_FILES,
            ),
            telemetry=telemetry,
        )
        return WorkspaceProjection(
            backend=workspace,
            routes={
                "/workspace/": WorkspaceRouteBackend(
                    workspace,
                    "/workspace/",
                    close_owner=True,
                ),
            },
        )


@dataclass
class SkillProjection:
    """主 Agent 和 SubAgent 可见的 Skill 路由。"""

    routes: dict[str, BackendProtocol] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    subagent_sources: dict[str, list[str]] = field(default_factory=dict)
    files: list[tuple[str, bytes]] = field(default_factory=list)


class SkillContextAssembler:
    """把已授权 Skill 投影成 DeepAgents 原生路径。"""

    def build(
        self,
        *,
        skills: list[Any],
        subagents: list[dict[str, Any]] | None,
    ) -> SkillProjection:
        """装配已标准化配置中的 SubAgent Skill 子集和只读路由。"""
        projection = SkillProjection()
        enabled_skills = [
            skill for skill in skills if getattr(skill, "enabled", True)
        ]
        if enabled_skills:
            projection.routes["/skills/"] = SkillCatalogBackend(enabled_skills)
            projection.sources = ["/skills/"]
            for skill in enabled_skills:
                projection.files.extend(
                    _package_files(
                        root=f"/skills/{skill.name}",
                        skill=skill,
                    )
                )

        normalized_subagents = list(subagents or [])
        skill_map = {skill.name: skill for skill in enabled_skills}
        for subagent_config in normalized_subagents:
            requested = subagent_config["skills"]
            missing = sorted(set(requested) - set(skill_map))
            if missing:
                raise SubAgentSpecError(
                    f"subagent {subagent_config['name']!r} 引用了未启用的 Skills: "
                    f"{', '.join(missing)}"
                )
            if not requested:
                continue
            name = subagent_config["name"]
            prefix = f"/subagent_skills/{name}/"
            selected = [skill_map[item] for item in requested]
            projection.routes[prefix] = SkillCatalogBackend(selected)
            projection.subagent_sources[name] = [prefix]
            for skill in selected:
                projection.files.extend(
                    _package_files(
                        root=f"{prefix}{skill.name}",
                        skill=skill,
                    )
                )
        return projection


def _package_files(*, root: str, skill: Any) -> list[tuple[str, bytes]]:
    """把可信 Skill 包转换为 Sandbox 上传文件。"""
    package_files = getattr(skill, "package_files", None) or {
        "SKILL.md": skill.skill_md,
    }
    files: list[tuple[str, bytes]] = []
    for relative, content in package_files.items():
        normalized = str(relative).replace("\\", "/").strip("/")
        if (
            not normalized
            or normalized.startswith("../")
            or "/../" in normalized
            or normalized in {".", ".."}
        ):
            raise ValueError(f"invalid Skill package path: {relative}")
        files.append(
            (
                f"{root.rstrip('/')}/{normalized}",
                str(content).encode("utf-8"),
            )
        )
    return files


@dataclass
class SandboxProjection:
    """当前运行的 Sandbox Backend。"""

    backend: SandboxBackendProtocol


class SandboxContextAssembler:
    """把 Skill、Memory 和 Workspace 初始文件装入官方 Sandbox。"""

    def build(
        self,
        *,
        settings: Any,
        workspace: MyDifyWorkspaceBackend,
        sandbox_name: str,
        skill_projection: SkillProjection,
        telemetry: AgentTelemetry,
    ) -> SandboxProjection | None:
        """按 provider 配置创建 Sandbox。"""
        backend = build_sandbox_backend(
            settings=settings,
            workspace=workspace,
            sandbox_name=sandbox_name,
            skill_files=skill_projection.files,
            telemetry=telemetry,
        )
        if backend is None:
            return None
        return SandboxProjection(backend=backend)


def build_filesystem_permissions(
    *,
    sandbox_enabled: bool = False,
) -> list[FilesystemPermission]:
    """构建非 Sandbox 模式的文件工具权限。"""
    if sandbox_enabled:
        # Sandbox 服务自身提供隔离边界，不能再向 execute 传入规则。
        return []

    return [
        FilesystemPermission(
            operations=["read", "write"],
            paths=[
                "/.system/**",
                "/.audit/**",
                "/workspace/.system/**",
                "/workspace/.audit/**",
            ],
            mode="deny",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=[
                "/skills/**",
                "/subagent_skills/**",
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
            ],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/workspace/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/memories/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/**"],
            mode="deny",
        ),
    ]


@inject
@dataclass
class AgentContextAssembler:
    """把业务数据投影成 DeepAgents 原生运行参数。"""

    checkpointer_provider: Callable[[], Any]
    store_provider: Callable[[], Any] | None = None

    def assemble(
        self,
        *,
        runtime_session: Session,
        run_context: AgentRunContext,
        skills: list[Any],
        subagents: list[dict[str, Any]] | None,
        settings: Config,
    ) -> AgentRuntimeResources:
        """按固定顺序构造一次运行的全部能力。"""
        # 1.创建本次运行独立的遥测上下文。
        telemetry = AgentTelemetry(
            LoggingAgentTelemetrySink(),
            attributes=run_context.telemetry_attributes,
        )
        workspace_projection: WorkspaceProjection | None = None
        sandbox_projection: SandboxProjection | None = None
        try:
            # 2.创建会话级 Workspace，后续文件能力都通过它暴露。
            workspace_projection = WorkspaceContextAssembler().build(
                runtime_session=runtime_session,
                run_context=run_context,
                settings=settings,
                telemetry=telemetry,
            )
            # 3.把已授权 Skill 和 SubAgent Skill 映射为虚拟路径。
            skill_projection = SkillContextAssembler().build(
                skills=skills,
                subagents=subagents,
            )
            # 4.创建租户隔离的长期记忆 Store 和文件路由。
            memory_projection = MemoryContextAssembler(
                store_provider=self.store_provider,
            ).build()
            # 5.按当前 provider 决定是否创建 Sandbox 隔离层。
            sandbox_projection = SandboxContextAssembler().build(
                settings=settings,
                workspace=workspace_projection.backend,
                sandbox_name=build_stable_sandbox_name(
                    account_id=run_context.account_id,
                    app_id=run_context.app_id,
                    conversation_id=run_context.conversation_id,
                    agent_id=run_context.agent_id,
                ),
                skill_projection=skill_projection,
                telemetry=telemetry,
            )
            # 6.合并 Workspace、Skill、Memory 和 Sandbox 路由。
            backend = build_composite_backend(
                routes={
                    **workspace_projection.routes,
                    **skill_projection.routes,
                    **memory_projection.routes,
                },
                sandbox=(
                    sandbox_projection.backend
                    if sandbox_projection is not None
                    else None
                ),
            )
            # 7.获取线程检查点，供 DeepAgents 恢复本次会话状态。
            checkpointer = self.checkpointer_provider()
        except Exception:
            if sandbox_projection is not None:
                sandbox_projection.backend.close()
            elif workspace_projection is not None:
                workspace_projection.backend.close()
            raise
        return AgentRuntimeResources(
            checkpointer=checkpointer,
            backend=backend,
            permissions=build_filesystem_permissions(
                sandbox_enabled=sandbox_projection is not None,
            ),
            skill_sources=skill_projection.sources,
            memory_sources=memory_projection.sources,
            subagent_skill_sources=skill_projection.subagent_sources,
            telemetry=telemetry,
            store=memory_projection.store,
            runtime_context=run_context,
        )


__all__ = [
    "AgentContextAssembler",
    "MemoryContextAssembler",
    "MemoryProjection",
    "SandboxContextAssembler",
    "SandboxProjection",
    "SkillContextAssembler",
    "SkillProjection",
    "WorkspaceContextAssembler",
    "WorkspaceProjection",
    "build_filesystem_permissions",
]
