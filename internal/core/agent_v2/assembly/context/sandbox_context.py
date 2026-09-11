"""Sandbox 运行时资源和文件权限装配。"""

from __future__ import annotations

from deepagents.middleware.filesystem import FilesystemPermission


def build_filesystem_permissions(
    *,
    memory_enabled: bool = False,
) -> list[FilesystemPermission]:
    """构建所有运行模式共用的文件工具权限。"""
    permissions = [
        FilesystemPermission(
            operations=["write"],
            paths=[
                "/skills",
                "/skills/**",
                "/subagent_skills",
                "/subagent_skills/**",
            ],
            mode="deny",
        ),
        FilesystemPermission(
            operations=["read"],
            paths=[
                "/",
                "/skills",
                "/skills/**",
                "/subagent_skills",
                "/subagent_skills/**",
                "/workspace",
                "/workspace/**",
            ],
            mode="allow",
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/workspace", "/workspace/**"],
            mode="allow",
        ),
    ]
    permissions.append(
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/memories", "/memories/**"],
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
    "build_filesystem_permissions",
]
