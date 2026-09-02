"""把 DeepAgents 本机 Shell Backend 接入 Agent Workspace。"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from deepagents.backends import LocalShellBackend
from deepagents.backends.protocol import ExecuteResponse

from internal.core.agent_v2.assembly.observability.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)
from internal.core.agent_v2.backends.sandbox.sandbox_workspace_sync import (
    SandboxProviderError,
    close_workspace_safely,
    seed_sandbox_workspace,
    sync_workspace_changes,
)
from internal.core.agent_v2.backends.workspace.workspace_backend import AgentWorkspaceBackend


_VIRTUAL_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_:/])/(?:workspace|skills|subagent_skills|tmp)(?=/|\s|['\"`]|$)"
)


class ManagedLocalShellBackend(LocalShellBackend):
    """开发环境本机 Backend；结束时把 Workspace 变更同步回权威存储。"""

    def __init__(
        self,
        *,
        workspace: AgentWorkspaceBackend,
        telemetry: AgentTelemetry | None = None,
        timeout: int = 1800,
        max_output_bytes: int = 500 * 1024,
    ) -> None:
        """创建隔离于项目目录的临时本机工作目录。"""
        self._workspace = workspace
        self._telemetry = telemetry or NOOP_AGENT_TELEMETRY
        self._root = Path(tempfile.mkdtemp(prefix="my-dify-local-shell-"))
        self._baseline_manifest: dict[str, str] = {}
        self._managed_closed = False
        try:
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
        except Exception:
            shutil.rmtree(self._root, ignore_errors=True)
            raise

    def set_workspace_baseline(self, manifest: dict[str, str]) -> None:
        """记录本次运行开始时的权威 Workspace 摘要。"""
        self._baseline_manifest = dict(manifest)

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """在本机 Shell 执行命令，并映射 Agent 虚拟路径到临时目录。"""
        translated = _translate_virtual_paths(command, self._root)
        return super().execute(translated, timeout=timeout)

    def close(self) -> None:
        """先同步 Workspace 增量，再删除临时目录和业务 Workspace 会话。"""
        if self._managed_closed:
            return
        sync_error: Exception | None = None
        try:
            sync_workspace_changes(
                backend=self,
                workspace=self._workspace,
                baseline_manifest=self._baseline_manifest,
            )
        except Exception as exc:  # noqa: BLE001
            sync_error = exc
            self._telemetry.emit(
                "sandbox.workspace_sync_failed",
                attributes={"sandbox_id": self.id},
            )
        finally:
            shutil.rmtree(self._root, ignore_errors=True)
            workspace_close_error = close_workspace_safely(
                self._workspace,
                telemetry=self._telemetry,
                sandbox_id=self.id,
            )
            if sync_error is None and workspace_close_error is not None:
                sync_error = workspace_close_error
            self._managed_closed = True
        if sync_error is not None:
            raise SandboxProviderError(
                f"failed to close local Shell Backend {self.id}"
            ) from sync_error

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
    workspace: AgentWorkspaceBackend,
    sandbox_name: str,
    skill_files: Iterable[tuple[str, bytes]],
    telemetry: AgentTelemetry | None = None,
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
            telemetry=telemetry,
            timeout=int(
                getattr(settings, "AGENT_SANDBOX_EXECUTE_TIMEOUT_SECONDS", 1800)
            ),
            max_output_bytes=int(
                getattr(settings, "AGENT_SANDBOX_MAX_OUTPUT_BYTES", 500 * 1024)
            ),
        )
        seed_sandbox_workspace(
            backend,
            workspace=workspace,
            skill_files=skill_files,
        )
        return backend
    except Exception as exc:  # noqa: BLE001
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
        else:
            close_workspace_safely(workspace, telemetry=telemetry)
        if isinstance(exc, SandboxProviderError):
            raise
        raise SandboxProviderError("failed to create local Shell Backend") from exc


def _translate_virtual_paths(command: str, root_dir: Path) -> str:
    """把常用 Agent 虚拟目录映射到本机临时目录。"""
    physical_roots = {
        name: (root_dir / name).as_posix()
        for name in ("workspace", "skills", "subagent_skills", "tmp")
    }

    def replace(match: re.Match[str]) -> str:
        virtual = match.group(0)
        name = virtual[1:].split("/", 1)[0]
        return physical_roots[name] + virtual[len(name) + 1 :]

    return _VIRTUAL_PATH_PATTERN.sub(replace, command)
