"""Sandbox 临时工作区与权威 Workspace 之间的同步逻辑。"""

from __future__ import annotations

from hashlib import sha256
from pathlib import PurePosixPath

from deepagents.backends.protocol import SandboxBackendProtocol

from internal.core.agent_v2.backends.sandbox_provider import SandboxProviderError
from internal.core.agent_v2.backends.workspace_backend import AgentWorkspaceBackend


def sync_workspace_changes(
    *,
    backend: SandboxBackendProtocol,
    workspace: AgentWorkspaceBackend,
    baseline_manifest: dict[str, str],
) -> None:
    """按摘要增量同步 Sandbox 产物，处理新增、变更和删除。"""
    paths: list[str] = []
    result = backend.glob("**/*", path="/workspace")
    if result.error:
        raise SandboxProviderError(result.error)
    for item in result.matches or []:
        if item.get("is_dir"):
            continue
        path = _normalize_sandbox_workspace_path(item.get("path"))
        if path is not None:
            paths.append(path)

    unique_paths = sorted(set(paths))
    responses = backend.download_files(unique_paths)
    current_files: dict[str, bytes] = {}
    for response in responses:
        if response.error or response.content is None:
            continue
        path = _normalize_sandbox_workspace_path(response.path)
        if path is not None:
            current_files[path] = response.content
    if len(current_files) != len(unique_paths):
        raise SandboxProviderError("failed to read final Sandbox Workspace")

    changed_files = [
        (path, content)
        for path, content in current_files.items()
        if baseline_manifest.get(path) != sha256(content).hexdigest()
    ]
    upload_responses = workspace.upload_files(changed_files)
    errors = [response for response in upload_responses if response.error]
    if errors:
        raise SandboxProviderError(f"failed to persist {len(errors)} Workspace files")

    deleted_paths = sorted(set(baseline_manifest) - set(current_files))
    delete_errors = workspace.delete_files(deleted_paths)
    if delete_errors:
        raise SandboxProviderError(
            f"failed to delete {len(delete_errors)} Workspace files"
        )


def sync_workspace_into_sandbox(
    *,
    backend: SandboxBackendProtocol,
    workspace: AgentWorkspaceBackend,
) -> dict[str, str]:
    """把权威 Workspace 文件播种到 Sandbox，并返回初始摘要。"""
    manifest = workspace.content_manifest()
    result = workspace.ls("/workspace")
    if result.error:
        raise SandboxProviderError(result.error)
    paths = sorted(
        {
            str(item["path"])
            for item in result.entries or []
            if not item.get("is_dir") and item.get("path")
        }
    )
    if not paths:
        return manifest

    responses = workspace.download_files(paths)
    files = [
        (response.path, response.content)
        for response in responses
        if response.content is not None and not response.error
    ]
    if len(files) != len(paths):
        raise SandboxProviderError("failed to read existing Workspace files")
    upload_responses = backend.upload_files(files)
    errors = [response for response in upload_responses if response.error]
    if errors:
        raise SandboxProviderError(f"failed to upload {len(errors)} Sandbox files")
    return manifest


def _normalize_sandbox_workspace_path(path: object) -> str | None:
    """统一 Provider 的 glob 路径，并拒绝脱离 /workspace 的结果。"""
    if not isinstance(path, str):
        return None
    raw = path.strip()
    if not raw or raw in {".", "./", "/workspace", "/workspace/"}:
        return None
    if "\x00" in raw or "\\" in raw:
        raise SandboxProviderError("Sandbox glob returned an invalid path")
    candidate = (
        PurePosixPath(raw)
        if raw.startswith("/")
        else PurePosixPath("/workspace", raw)
    )
    parts = candidate.parts
    if len(parts) < 3 or parts[0] != "/" or parts[1] != "workspace":
        raise SandboxProviderError("Sandbox glob returned a path outside /workspace")
    if any(part in {"", ".", "..", "~"} for part in parts[2:]):
        raise SandboxProviderError("Sandbox glob returned an unsafe workspace path")
    return candidate.as_posix()


__all__ = ["sync_workspace_changes", "sync_workspace_into_sandbox"]
