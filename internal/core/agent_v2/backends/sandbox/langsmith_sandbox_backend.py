"""LangSmith Sandbox Backend 的生命周期和初始化适配。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from deepagents.backends import LangSmithSandbox
from deepagents.backends.protocol import SandboxBackendProtocol

from internal.core.agent_v2.backends.sandbox.sandbox_workspace_sync import (
    SandboxProviderError,
    close_workspace_safely,
    seed_sandbox_workspace,
    sync_workspace_changes,
)
from internal.core.agent_v2.backends.workspace.workspace_backend import AgentWorkspaceBackend
from internal.core.agent_v2.assembly.observability.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)


class ManagedLangSmithSandbox(LangSmithSandbox):
    """带 MyDify Workspace 同步和生命周期管理的官方 Sandbox Backend。"""

    def __init__(
        self,
        *,
        sandbox: Any,
        client: Any,
        workspace: AgentWorkspaceBackend,
        telemetry: AgentTelemetry | None = None,
    ) -> None:
        super().__init__(sandbox)
        self._client = client
        self._workspace = workspace
        self._telemetry = telemetry or NOOP_AGENT_TELEMETRY
        self._closed = False
        self._remote_deleted = False
        self._baseline_manifest: dict[str, str] = {}

    def set_workspace_baseline(self, manifest: dict[str, str]) -> None:
        """记录本次运行开始时的权威 Workspace 清单。"""
        self._baseline_manifest = dict(manifest)

    def close(self) -> None:
        """先回收工作文件，再删除远程 Sandbox。"""
        if self._closed:
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
            workspace_close_error = close_workspace_safely(
                self._workspace,
                telemetry=self._telemetry,
                sandbox_id=self.id,
            )
            if sync_error is None and workspace_close_error is not None:
                sync_error = workspace_close_error
            self._closed = True
        if sync_error is not None:
            raise SandboxProviderError(
                f"failed to close Sandbox {self.id}"
            ) from sync_error

    def delete_remote(self) -> None:
        """在会话或应用删除时显式清理远程 Sandbox。"""
        if self._remote_deleted:
            return
        from langsmith.sandbox import ResourceNotFoundError

        try:
            self._client.delete_sandbox(self.id)
        except ResourceNotFoundError:
            self._remote_deleted = True
            return
        except Exception as exc:  # noqa: BLE001
            self._telemetry.emit(
                "sandbox.delete_failed",
                attributes={"sandbox_id": self.id},
            )
            raise SandboxProviderError(f"failed to delete Sandbox {self.id}") from exc
        self._remote_deleted = True


def create_backend(
    *,
    settings: Any,
    workspace: AgentWorkspaceBackend,
    sandbox_name: str,
    skill_files: Iterable[tuple[str, bytes]],
    telemetry: AgentTelemetry | None = None,
) -> SandboxBackendProtocol:
    """获取或创建 LangSmith Sandbox，并完成可复用实例的重置。"""
    from langsmith.sandbox import ResourceNotFoundError, SandboxClient

    client = SandboxClient()
    sandbox = None
    backend: ManagedLangSmithSandbox | None = None
    created = False
    try:
        try:
            sandbox = client.get_sandbox(sandbox_name)
        except ResourceNotFoundError:
            created = True
            sandbox = client.create_sandbox(
                **_langsmith_create_kwargs(settings, sandbox_name)
            )

        if str(getattr(sandbox, "status", "")).lower() == "stopped":
            sandbox.start()
        backend = ManagedLangSmithSandbox(
            sandbox=sandbox,
            client=client,
            workspace=workspace,
            telemetry=telemetry,
        )
        _reset_sandbox_projection(backend)
        seed_sandbox_workspace(
            backend,
            workspace=workspace,
            skill_files=list(skill_files),
            protect_skills=True,
        )
        return backend
    except Exception as exc:  # noqa: BLE001
        _cleanup_initialization(
            backend=backend,
            client=client,
            sandbox=sandbox,
            created=created,
            workspace=workspace,
            telemetry=telemetry,
        )
        if isinstance(exc, SandboxProviderError):
            raise
        raise SandboxProviderError("failed to create LangSmith Sandbox") from exc


def _langsmith_create_kwargs(settings: Any, sandbox_name: str) -> dict[str, Any]:
    """集中构造 LangSmith 创建参数，避免配置读取散落在流程中。"""
    kwargs: dict[str, Any] = {
        "name": sandbox_name,
        "idle_ttl_seconds": getattr(
            settings,
            "AGENT_SANDBOX_IDLE_TTL_SECONDS",
            600,
        ),
        "delete_after_stop_seconds": getattr(
            settings,
            "AGENT_SANDBOX_DELETE_AFTER_STOP_SECONDS",
            86400,
        ),
    }
    snapshot_id = getattr(settings, "AGENT_SANDBOX_SNAPSHOT_ID", None)
    if snapshot_id:
        kwargs["snapshot_id"] = snapshot_id
    return kwargs


def _reset_sandbox_projection(backend: ManagedLangSmithSandbox) -> None:
    """清空可复用 Sandbox 中上次运行留下的业务投影。"""
    command = (
        "mkdir -p /workspace && "
        "find /workspace -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + && "
        "rm -rf -- /skills /subagent_skills"
    )
    result = backend.execute(command)
    if result.exit_code not in (0, None):
        raise SandboxProviderError("failed to reset Sandbox projections")


def _cleanup_initialization(
    *,
    backend: ManagedLangSmithSandbox | None,
    client: Any,
    sandbox: Any | None,
    created: bool,
    workspace: AgentWorkspaceBackend,
    telemetry: AgentTelemetry | None,
) -> None:
    """清理 LangSmith 初始化中已创建的远程和本地资源。"""
    if backend is not None:
        _close_backend_quietly(backend)
        if created:
            try:
                backend.delete_remote()
            except Exception:  # noqa: BLE001
                pass
        return

    sandbox_id = getattr(sandbox, "id", None)
    if sandbox is not None and created:
        try:
            client.delete_sandbox(sandbox.name)
        except Exception:  # noqa: BLE001
            pass
    close_workspace_safely(
        workspace,
        telemetry=telemetry,
        sandbox_id=sandbox_id,
    )


def _close_backend_quietly(backend: Any) -> None:
    """初始化失败时尽力关闭已创建 Backend，不覆盖原始异常。"""
    try:
        backend.close()
    except Exception:  # noqa: BLE001
        pass
