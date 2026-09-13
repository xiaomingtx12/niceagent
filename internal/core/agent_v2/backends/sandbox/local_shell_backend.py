"""把 DeepAgents 本机 Shell Backend 接入 Agent Workspace。"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from deepagents.backends import LocalShellBackend

from internal.core.agent_v2.backends.sandbox.sandbox_initialization import (
    SandboxProviderError,
)
from internal.core.agent_v2.backends.workspace.workspace_projection import WorkspaceProjection
from internal.core.agent_v2.backends.sandbox.sandbox_skill_upload import seed_sandbox_skills


class ManagedLocalShellBackend(LocalShellBackend):
    """本机 Shell 与文件工具共享本轮目录，持久化由显式交付负责。"""

    execution_uses_virtual_paths = True

    def __init__(
        self,
        *,
        workspace: WorkspaceProjection,
        timeout: int = 1800,
        max_output_bytes: int = 500 * 1024,
    ) -> None:
        """创建隔离于项目目录的临时本机工作目录。"""
        self._root = workspace.root
        super().__init__(
            root_dir=self._root,
            virtual_mode=True,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
            # 本机开发需要沿用当前 Python、Node 等工具的 PATH。
            inherit_env=True,
        )
        for name in ("workspace", "skills", "subagent_skills", "tmp"):
            (self._root / name).mkdir(parents=True, exist_ok=True)
        self._install_windows_python_alias()

    def _install_windows_python_alias(self) -> None:
        """让 Windows 下常见的 ``python3`` 命令指向当前服务解释器。"""
        if os.name != "nt":
            return
        alias = self._root / "python3.cmd"
        executable = str(Path(sys.executable))
        alias.write_text(
            f'@echo off\r\n"{executable}" %*\r\n',
            encoding="utf-8",
            newline="",
        )


def create_backend(
    *,
    settings: Any,
    workspace: WorkspaceProjection,
    sandbox_name: str,
    skill_files: Iterable[tuple[str, bytes]],
) -> ManagedLocalShellBackend:
    """创建仅供非生产环境使用的本机 Shell Backend。"""
    del sandbox_name
    if os.getenv("FLASK_ENV", "").lower() == "production":
        raise SandboxProviderError(
            "local_shell 仅允许在开发环境使用，生产环境请配置隔离 Sandbox"
        )
    if getattr(settings, "AGENT_SANDBOX_SNAPSHOT_ID", None):
        raise SandboxProviderError(
            "AGENT_SANDBOX_SNAPSHOT_ID 仅适用于 langsmith Sandbox"
        )

    backend: ManagedLocalShellBackend | None = None
    try:
        backend = ManagedLocalShellBackend(
            workspace=workspace,
            timeout=int(
                getattr(settings, "AGENT_SANDBOX_EXECUTE_TIMEOUT_SECONDS", 1800)
            ),
            max_output_bytes=int(
                getattr(settings, "AGENT_SANDBOX_MAX_OUTPUT_BYTES", 500 * 1024)
            ),
        )
        seed_sandbox_skills(backend, list(skill_files))
        return backend
    except BaseException as exc:
        # 借用的本轮目录由装配失败清理统一释放，本机执行器没有额外连接。
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, SandboxProviderError):
            raise
        raise SandboxProviderError("failed to create local Shell Backend") from exc
