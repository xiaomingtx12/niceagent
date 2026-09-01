"""DeepAgents 官方 Sandbox provider 的生命周期适配。"""

from __future__ import annotations

from collections.abc import Iterable
from hashlib import sha256
from typing import Any

from deepagents.backends import LangSmithSandbox
from deepagents.backends.protocol import (
    FileUploadResponse,
    SandboxBackendProtocol,
)

from internal.core.agent_v2.backends.sandbox_provider import SandboxProviderError
from internal.core.agent_v2.backends.sandbox_workspace_sync import (
    close_workspace_safely,
    make_skill_projection_read_only,
    sync_workspace_changes,
    sync_workspace_into_sandbox,
)
from internal.core.agent_v2.backends.workspace_backend import AgentWorkspaceBackend
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
            self._sync_workspace_from_sandbox()
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

    def _sync_workspace_from_sandbox(self) -> None:
        """只把新增、变更和删除同步回权威 Workspace。"""
        sync_workspace_changes(
            backend=self,
            workspace=self._workspace,
            baseline_manifest=self._baseline_manifest,
        )


def build_sandbox_backend(
    *,
    settings: Any,
    workspace: AgentWorkspaceBackend,
    sandbox_name: str,
    skill_files: Iterable[tuple[str, bytes]],
    telemetry: AgentTelemetry | None = None,
) -> SandboxBackendProtocol | None:
    """按配置创建 Sandbox，并上传本次运行所需的初始文件。

    ``bubblewrap`` 是 4 核 4G 单机部署的默认推荐 Provider；LangSmith 和
    Daytona 作为可选的托管 Provider 保留，``local_shell`` 仅供 Windows 开发联调。
    所有启用的 Provider 都使用同一套 Workspace 增量同步。
    """
    provider = str(getattr(settings, "AGENT_SANDBOX_PROVIDER", "disabled")).lower()
    if provider in {"disabled", "none", "off", "false"}:
        return None
    if provider in {"bubblewrap", "bwrap"}:
        if getattr(settings, "AGENT_SANDBOX_SNAPSHOT_ID", None):
            raise SandboxProviderError(
                "AGENT_SANDBOX_SNAPSHOT_ID 仅适用于 langsmith Sandbox"
            )
        from internal.core.agent_v2.backends.managed_bubblewrap_sandbox import (
            build_bubblewrap_backend,
        )

        backend = build_bubblewrap_backend(
            settings=settings,
            workspace=workspace,
            telemetry=telemetry,
        )
        try:
            baseline = sync_workspace_into_sandbox(
                backend=backend,
                workspace=workspace,
            )
            backend.set_workspace_baseline(baseline)
            files = list(skill_files)
            if files:
                responses = backend.upload_files(files)
                _raise_for_upload_errors(responses)
            return backend
        except Exception as exc:  # noqa: BLE001
            try:
                backend.close()
            except Exception:
                pass
            if isinstance(exc, SandboxProviderError):
                raise
            raise SandboxProviderError(
                "failed to initialize Bubblewrap Sandbox"
            ) from exc
    if provider == "local_shell":
        if getattr(settings, "AGENT_SANDBOX_SNAPSHOT_ID", None):
            raise SandboxProviderError(
                "AGENT_SANDBOX_SNAPSHOT_ID 仅适用于 langsmith Sandbox"
            )
        from internal.core.agent_v2.backends.local_shell_backend import (
            build_local_shell_backend,
        )

        return build_local_shell_backend(
            settings=settings,
            workspace=workspace,
            skill_files=list(skill_files),
            telemetry=telemetry,
        )
    if provider == "daytona":
        try:
            from internal.core.agent_v2.backends.daytona_sandbox_backend import (
                build_daytona_backend,
            )
        except ImportError as exc:
            raise SandboxProviderError(
                "Daytona provider requires langchain-daytona and daytona"
            ) from exc

        return build_daytona_backend(
            settings=settings,
            workspace=workspace,
            skill_files=list(skill_files),
            telemetry=telemetry,
        )
    if provider != "langsmith":
        raise SandboxProviderError(f"unsupported AGENT_SANDBOX_PROVIDER: {provider}")

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
            snapshot_id = getattr(settings, "AGENT_SANDBOX_SNAPSHOT_ID", None)
            create_kwargs: dict[str, Any] = {
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
            if snapshot_id:
                create_kwargs["snapshot_id"] = snapshot_id
            sandbox = client.create_sandbox(**create_kwargs)

        if str(getattr(sandbox, "status", "")).lower() == "stopped":
            sandbox.start()
        backend = ManagedLangSmithSandbox(
            sandbox=sandbox,
            client=client,
            workspace=workspace,
            telemetry=telemetry,
        )
        _reset_sandbox_projection(backend)
        baseline = sync_workspace_into_sandbox(
            backend=backend,
            workspace=workspace,
        )
        backend.set_workspace_baseline(baseline)
        files = list(skill_files)
        if files:
            responses = backend.upload_files(files)
            _raise_for_upload_errors(responses)
            make_skill_projection_read_only(backend)
        return backend
    except Exception as exc:  # noqa: BLE001
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
            if created:
                try:
                    backend.delete_remote()
                except Exception:
                    pass
        elif sandbox is not None:
            if created:
                try:
                    client.delete_sandbox(sandbox.name)
                except Exception:
                    pass
            close_workspace_safely(
                workspace,
                telemetry=telemetry,
                sandbox_id=getattr(sandbox, "id", None),
            )
        else:
            close_workspace_safely(workspace, telemetry=telemetry)
        if isinstance(exc, SandboxProviderError):
            raise
        raise SandboxProviderError("failed to create LangSmith Sandbox") from exc


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


def _raise_for_upload_errors(
    responses: Iterable[FileUploadResponse],
) -> None:
    """把官方批量上传的逐文件错误转换为装配失败。"""
    errors = [response for response in responses if response.error]
    if errors:
        raise SandboxProviderError(f"failed to upload {len(errors)} Sandbox files")


def build_stable_sandbox_name(
    *,
    account_id: object,
    app_id: object,
    conversation_id: object,
    agent_id: str,
) -> str:
    """根据线程身份生成脱敏且可复用的 Sandbox 名称。"""
    scope = f"mydify:{account_id}:{app_id}:{conversation_id}:{agent_id}"
    digest = sha256(scope.encode("utf-8")).hexdigest()[:32]
    return f"mydify-sandbox-{digest}"


__all__ = [
    "ManagedLangSmithSandbox",
    "SandboxProviderError",
    "build_stable_sandbox_name",
    "build_sandbox_backend",
]
