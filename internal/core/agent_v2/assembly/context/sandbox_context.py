"""Sandbox 运行时资源和文件权限装配。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from deepagents.backends.protocol import SandboxBackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission

from internal.core.agent_v2.backends.langsmith_sandbox_backend import (
    build_sandbox_backend,
)
from internal.core.agent_v2.backends.workspace_backend import AgentWorkspaceBackend
from internal.core.agent_v2.assembly.context.skill_context import SkillProjection
from internal.core.agent_v2.assembly.observability.telemetry import AgentTelemetry


@dataclass
class SandboxProjection:
    """当前运行的 Sandbox Backend。"""

    backend: SandboxBackendProtocol


class SandboxContextAssembler:
    """把 Skill 和 Workspace 初始文件装入官方 Sandbox。"""

    def build(
        self,
        *,
        settings: Any,
        workspace: AgentWorkspaceBackend,
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
    memory_enabled: bool = False,
) -> list[FilesystemPermission]:
    """构建所有运行模式共用的文件工具权限。"""
    permissions = [
        FilesystemPermission(
            operations=["write"],
            paths=["/skills/**", "/subagent_skills/**"],
            mode="deny",
        ),
        FilesystemPermission(
            operations=["read"],
            paths=["/skills/**", "/subagent_skills/**", "/workspace/**"],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/workspace/**"],
            mode="allow",
        ),
    ]
    permissions.append(
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/memories/**"],
            mode="allow" if memory_enabled else "deny",
        )
    )
    permissions.append(
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/**"],
            mode="deny",
        )
    )
    return permissions


__all__ = [
    "SandboxContextAssembler",
    "SandboxProjection",
    "build_filesystem_permissions",
]
