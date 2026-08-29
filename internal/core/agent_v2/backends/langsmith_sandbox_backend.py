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

from internal.core.agent_v2.backends.workspace_backend import AgentWorkspaceBackend
from internal.core.agent_v2.runtime.observability.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)


class SandboxProviderError(RuntimeError):
    """Sandbox provider 创建、文件同步或清理失败。"""


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
        self._closed = True
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
            self._workspace.close()
        if sync_error is not None:
            raise SandboxProviderError(
                f"failed to close Sandbox {self.id}"
            ) from sync_error

    def delete_remote(self) -> None:
        """在会话或应用删除时显式清理远程 Sandbox。"""
        if self._remote_deleted:
            return
        self._remote_deleted = True
        from langsmith.sandbox import ResourceNotFoundError

        try:
            self._client.delete_sandbox(self.id)
        except ResourceNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001
            self._telemetry.emit(
                "sandbox.delete_failed",
                attributes={"sandbox_id": self.id},
            )
            raise SandboxProviderError(f"failed to delete Sandbox {self.id}") from exc

    def _sync_workspace_from_sandbox(self) -> None:
        """只把新增、变更和删除同步回权威 Workspace。"""
        paths: list[str] = []
        for prefix in ("/workspace",):
            result = self.glob("**/*", path=prefix)
            if result.error:
                raise SandboxProviderError(result.error)
            for item in result.matches or []:
                path = str(item.get("path", ""))
                if path and not path.endswith("/"):
                    paths.append(path)
        responses = self.download_files(sorted(set(paths)))
        current_files = {
            response.path: response.content
            for response in responses
            if response.content is not None and not response.error
        }
        if len(current_files) != len(set(paths)):
            raise SandboxProviderError("failed to read final Sandbox Workspace")

        changed_files = [
            (path, content)
            for path, content in current_files.items()
            if self._baseline_manifest.get(path) != sha256(content).hexdigest()
        ]
        upload_responses = self._workspace.upload_files(changed_files)
        errors = [response for response in upload_responses if response.error]
        if errors:
            raise SandboxProviderError(
                f"failed to persist {len(errors)} Workspace files"
            )
        deleted_paths = sorted(set(self._baseline_manifest) - set(current_files))
        delete_errors = self._workspace.delete_files(deleted_paths)
        if delete_errors:
            raise SandboxProviderError(
                f"failed to delete {len(delete_errors)} Workspace files"
            )


def build_sandbox_backend(
    *,
    settings: Any,
    workspace: AgentWorkspaceBackend,
    sandbox_name: str,
    skill_files: Iterable[tuple[str, bytes]],
    telemetry: AgentTelemetry | None = None,
) -> SandboxBackendProtocol | None:
    """按配置创建官方 Sandbox，并上传本次运行所需的初始文件。"""
    provider = str(getattr(settings, "AGENT_SANDBOX_PROVIDER", "disabled")).lower()
    if provider in {"disabled", "none", "off", "false"}:
        return None
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
        baseline = _sync_workspace_into_sandbox(backend, workspace)
        backend.set_workspace_baseline(baseline)
        files = list(skill_files)
        if files:
            responses = backend.upload_files(files)
            _raise_for_upload_errors(responses)
            _make_skill_projection_read_only(backend)
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
            workspace.close()
        else:
            workspace.close()
        if isinstance(exc, SandboxProviderError):
            raise
        raise SandboxProviderError("failed to create LangSmith Sandbox") from exc


def _sync_workspace_into_sandbox(
    backend: ManagedLangSmithSandbox,
    workspace: AgentWorkspaceBackend,
) -> dict[str, str]:
    """把 COS 权威文件放入 Sandbox，并返回初始内容清单。"""
    manifest = workspace.content_manifest()
    paths: list[str] = []
    for prefix in ("/workspace",):
        result = workspace.ls(prefix)
        if result.error:
            raise SandboxProviderError(result.error)
        paths.extend(
            str(item["path"])
            for item in result.entries or []
            if not item.get("is_dir") and item.get("path")
        )
    if not paths:
        return manifest
    responses = workspace.download_files(sorted(set(paths)))
    files = [
        (response.path, response.content)
        for response in responses
        if response.content is not None and not response.error
    ]
    if len(files) != len(paths):
        raise SandboxProviderError("failed to read existing Workspace files")
    upload_responses = backend.upload_files(files)
    _raise_for_upload_errors(upload_responses)
    return manifest


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


def _make_skill_projection_read_only(backend: ManagedLangSmithSandbox) -> None:
    """把 Sandbox 中的 Skill 副本设置为操作系统只读。"""
    command = (
        "for path in /skills /subagent_skills; do "
        'if [ -e "$path" ]; then chmod -R a-w -- "$path"; fi; '
        "done"
    )
    result = backend.execute(command)
    if result.exit_code not in (0, None):
        raise SandboxProviderError("failed to protect Sandbox Skill files")


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
