"""Daytona Sandbox 的 Deep Agents 生命周期适配。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from daytona import Daytona, DaytonaConfig
from daytona.common.errors import DaytonaError
from langchain_daytona import DaytonaSandbox

from internal.core.agent_v2.assembly.observability.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)
from internal.core.agent_v2.backends.sandbox.sandbox_workspace_sync import (
    SandboxProviderError,
    close_workspace_safely,
    ensure_sandbox_workspace,
    seed_sandbox_workspace,
    sync_workspace_changes,
)
from internal.core.agent_v2.backends.workspace.workspace_backend import AgentWorkspaceBackend


class ManagedDaytonaSandbox(DaytonaSandbox):
    """一次 Run 独占的 Daytona Sandbox，结束时同步并停止远程资源。

    Daytona Sandbox 不跨对话复用；Workspace/COS 是持久化权威来源，远程
    Sandbox 只承载当前 Run 的临时执行状态。
    """

    def __init__(
        self,
        *,
        sandbox: Any,
        workspace: AgentWorkspaceBackend,
        timeout: int = 1800,
        telemetry: AgentTelemetry | None = None,
    ) -> None:
        super().__init__(sandbox=sandbox, timeout=timeout)
        self._sandbox = sandbox
        self._workspace = workspace
        self._telemetry = telemetry or NOOP_AGENT_TELEMETRY
        self._baseline_manifest: dict[str, str] = {}
        self._closed = False

    def set_workspace_baseline(self, manifest: dict[str, str]) -> None:
        """记录运行开始时的权威 Workspace 清单。"""
        self._baseline_manifest = dict(manifest)

    def close(self) -> None:
        """同步 Workspace 后停止 Daytona Sandbox；清理动作幂等。"""
        if self._closed:
            return

        close_error: Exception | None = None
        try:
            sync_workspace_changes(
                backend=self,
                workspace=self._workspace,
                baseline_manifest=self._baseline_manifest,
            )
        except Exception as exc:  # noqa: BLE001
            close_error = exc
            self._telemetry.emit(
                "sandbox.workspace_sync_failed",
                attributes={"sandbox_id": self.id},
            )
        finally:
            try:
                self._sandbox.stop()
            except Exception as exc:  # noqa: BLE001
                if close_error is None:
                    close_error = exc
                self._telemetry.emit(
                    "sandbox.stop_failed",
                    attributes={"sandbox_id": self.id},
                )
            workspace_close_error = close_workspace_safely(
                self._workspace,
                telemetry=self._telemetry,
                sandbox_id=self.id,
            )
            if close_error is None and workspace_close_error is not None:
                close_error = workspace_close_error
            self._closed = True

        if close_error is not None:
            raise SandboxProviderError(
                f"failed to close Daytona Sandbox {self.id}"
            ) from close_error


def create_backend(
    *,
    settings: Any,
    workspace: AgentWorkspaceBackend,
    sandbox_name: str,
    skill_files: Iterable[tuple[str, bytes]],
    telemetry: AgentTelemetry | None = None,
) -> ManagedDaytonaSandbox:
    """创建一次性 Daytona Sandbox，并播种 Workspace 与 Skill 文件。"""
    del sandbox_name
    sandbox: Any | None = None
    backend: ManagedDaytonaSandbox | None = None
    try:
        if getattr(settings, "AGENT_SANDBOX_SNAPSHOT_ID", None):
            raise SandboxProviderError(
                "AGENT_SANDBOX_SNAPSHOT_ID 仅适用于 langsmith Sandbox"
            )

        api_key = str(getattr(settings, "DAYTONA_API_KEY", "") or "").strip()
        api_url = str(getattr(settings, "DAYTONA_API_URL", "") or "").strip()
        client = Daytona(
            DaytonaConfig(
                api_key=api_key or None,
                api_url=api_url or None,
            )
        )
        sandbox = client.create()
        backend = ManagedDaytonaSandbox(
            sandbox=sandbox,
            workspace=workspace,
            timeout=int(
                getattr(settings, "AGENT_SANDBOX_EXECUTE_TIMEOUT_SECONDS", 1800)
            ),
            telemetry=telemetry,
        )
        ensure_sandbox_workspace(backend)
        seed_sandbox_workspace(
            backend,
            workspace=workspace,
            skill_files=skill_files,
            protect_skills=True,
        )
        return backend
    except Exception as exc:  # noqa: BLE001
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
        elif sandbox is not None:
            try:
                sandbox.stop()
            except Exception:
                telemetry_obj = telemetry or NOOP_AGENT_TELEMETRY
                telemetry_obj.emit(
                    "sandbox.stop_failed",
                    attributes={"sandbox_id": getattr(sandbox, "id", None)},
                )
            close_workspace_safely(
                workspace,
                telemetry=telemetry,
                sandbox_id=getattr(sandbox, "id", None),
            )
        else:
            close_workspace_safely(workspace, telemetry=telemetry)
        if isinstance(exc, SandboxProviderError):
            raise
        if isinstance(exc, DaytonaError):
            detail = str(exc).strip()
            message = "failed to create Daytona Sandbox"
            if detail:
                message = f"{message}: {detail}"
            raise SandboxProviderError(message) from exc
        raise SandboxProviderError("failed to create Daytona Sandbox") from exc
