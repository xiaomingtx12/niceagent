"""Shared lifecycle cleanup for managed sandbox backends."""

from __future__ import annotations

from collections.abc import Callable
from deepagents.backends.protocol import SandboxBackendProtocol

from internal.core.agent_v2.observability.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)
from internal.core.agent_v2.backends.sandbox.sandbox_workspace_sync import (
    SandboxProviderError,
    close_workspace_safely,
    sync_workspace_changes,
)
from internal.core.agent_v2.backends.workspace.workspace_backend import WorkspaceBackend
from internal.core.agent_v2.entity.workspace_snapshot import WorkspaceManifest


def close_managed_sandbox(
    *,
    backend: SandboxBackendProtocol,
    workspace: WorkspaceBackend,
    baseline_manifest: WorkspaceManifest | None,
    close_provider: Callable[[], None],
    telemetry: AgentTelemetry | None,
    sandbox_id: str,
    provider_name: str,
) -> None:
    """Synchronize and close a provider while preserving the first failure.

    Provider implementations only supply ``close_provider``. Workspace
    synchronization and cleanup ordering stay identical for every provider.
    """
    sync_error: Exception | None = None
    telemetry_obj = telemetry or NOOP_AGENT_TELEMETRY
    try:
        # A missing baseline means initialization failed; never persist a partial seed.
        if baseline_manifest is not None:
            sync_workspace_changes(
                backend=backend,
                workspace=workspace,
                baseline_manifest=baseline_manifest,
            )
    except Exception as exc:  # noqa: BLE001
        sync_error = exc
        telemetry_obj.emit(
            "sandbox.workspace_sync_failed",
            attributes={"sandbox_id": sandbox_id},
        )
    finally:
        try:
            close_provider()
        except Exception as exc:  # noqa: BLE001
            if sync_error is None:
                sync_error = exc
            telemetry_obj.emit(
                "sandbox.provider_close_failed",
                attributes={"sandbox_id": sandbox_id, "provider": provider_name},
            )
        workspace_close_error = close_workspace_safely(
            workspace,
            telemetry=telemetry_obj,
            sandbox_id=sandbox_id,
        )
        if sync_error is None and workspace_close_error is not None:
            sync_error = workspace_close_error

    if sync_error is not None:
        raise SandboxProviderError(
            f"failed to close {provider_name} Sandbox {sandbox_id}: {sync_error}"
        ) from sync_error


__all__ = ["close_managed_sandbox"]
