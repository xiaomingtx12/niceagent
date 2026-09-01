"""把 Bubblewrap Sandbox 与 Workspace 权威存储连接起来。"""

from __future__ import annotations

from typing import Any

from internal.core.agent_v2.assembly.observability.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)
from internal.core.agent_v2.backends.bubblewrap_sandbox_backend import (
    BubblewrapSandbox,
)
from internal.core.agent_v2.backends.sandbox_provider import SandboxProviderError
from internal.core.agent_v2.backends.sandbox_workspace_sync import (
    sync_workspace_changes,
)
from internal.core.agent_v2.backends.workspace_backend import AgentWorkspaceBackend


class ManagedBubblewrapSandbox(BubblewrapSandbox):
    """一次性本机 Sandbox，结束时将 Workspace 增量写回 COS。"""

    def __init__(
        self,
        *,
        workspace: AgentWorkspaceBackend,
        telemetry: AgentTelemetry | None = None,
        allow_network: bool = False,
        timeout: int = 1800,
        max_output_bytes: int = 500 * 1024,
    ) -> None:
        super().__init__(
            allow_network=allow_network,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
        )
        self._workspace = workspace
        self._telemetry = telemetry or NOOP_AGENT_TELEMETRY
        self._baseline_manifest: dict[str, str] = {}
        self._managed_closed = False

    def set_workspace_baseline(self, manifest: dict[str, str]) -> None:
        """记录运行开始时的权威 Workspace 摘要。"""
        self._baseline_manifest = dict(manifest)

    def close(self) -> None:
        """先同步增量产物，再关闭本机目录和 Workspace 会话。"""
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
            try:
                super().close()
            except Exception as exc:  # noqa: BLE001
                if sync_error is None:
                    sync_error = exc
            try:
                self._workspace.close()
            except Exception as exc:  # noqa: BLE001
                if sync_error is None:
                    sync_error = exc
                self._telemetry.emit(
                    "sandbox.workspace_close_failed",
                    attributes={"sandbox_id": self.id},
                )
            finally:
                self._managed_closed = True
        if sync_error is not None:
            raise SandboxProviderError(
                f"failed to close Sandbox {self.id}"
            ) from sync_error


def build_bubblewrap_backend(
    *,
    settings: Any,
    workspace: AgentWorkspaceBackend,
    telemetry: AgentTelemetry | None = None,
) -> ManagedBubblewrapSandbox:
    """创建本机 Sandbox；所有输入仍由宿主显式播种。"""
    try:
        network_setting = getattr(settings, "AGENT_SANDBOX_ALLOW_NETWORK", False)
        allow_network = (
            network_setting.lower() == "true"
            if isinstance(network_setting, str)
            else bool(network_setting)
        )
        backend = ManagedBubblewrapSandbox(
            workspace=workspace,
            telemetry=telemetry,
            allow_network=allow_network,
            timeout=int(
                getattr(settings, "AGENT_SANDBOX_EXECUTE_TIMEOUT_SECONDS", 1800)
            ),
            max_output_bytes=int(
                getattr(settings, "AGENT_SANDBOX_MAX_OUTPUT_BYTES", 500 * 1024)
            ),
        )
        return backend
    except Exception as exc:  # noqa: BLE001
        workspace.close()
        if isinstance(exc, SandboxProviderError):
            raise
        raise SandboxProviderError("failed to create Bubblewrap Sandbox") from exc


__all__ = ["ManagedBubblewrapSandbox", "build_bubblewrap_backend"]
