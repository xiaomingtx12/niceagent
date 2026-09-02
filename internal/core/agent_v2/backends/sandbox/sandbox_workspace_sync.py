"""Sandbox 临时工作区与权威 Workspace 之间的同步逻辑。"""

from __future__ import annotations

from collections.abc import Iterable
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any

from deepagents.backends.protocol import SandboxBackendProtocol

from internal.core.agent_v2.assembly.observability.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)
from internal.core.agent_v2.backends.workspace.workspace_backend import AgentWorkspaceBackend


class SandboxProviderError(RuntimeError):
    """Sandbox provider 创建、文件同步或清理失败。"""


def close_workspace_safely(
    workspace: AgentWorkspaceBackend,
    *,
    telemetry: AgentTelemetry | None = None,
    sandbox_id: str | None = None,
) -> Exception | None:
    """关闭 Workspace；返回清理异常，避免覆盖主异常。"""
    try:
        workspace.close()
    except Exception as exc:  # noqa: BLE001
        (telemetry or NOOP_AGENT_TELEMETRY).emit(
            "sandbox.workspace_close_failed",
            attributes={"sandbox_id": sandbox_id},
        )
        return exc
    return None


def ensure_sandbox_workspace(backend: SandboxBackendProtocol) -> None:
    """确保远程 Provider 提供可用的权威 Workspace 目录。"""
    result = backend.execute("mkdir -p /workspace")
    if result.exit_code not in (0, None):
        raise SandboxProviderError("failed to create Sandbox Workspace directory")


def make_skill_projection_read_only(backend: SandboxBackendProtocol) -> None:
    """把 Sandbox 中的 Skill 副本设置为操作系统只读。"""
    command = (
        "for path in /skills /subagent_skills; do "
        'if [ -e "$path" ]; then chmod -R a-w -- "$path"; fi; '
        "done"
    )
    result = backend.execute(command)
    if result.exit_code not in (0, None):
        raise SandboxProviderError("failed to protect Sandbox Skill files")


def seed_sandbox_workspace(
    backend: Any,
    *,
    workspace: AgentWorkspaceBackend,
    skill_files: list[tuple[str, bytes]],
    protect_skills: bool = False,
) -> None:
    """把权威 Workspace 和 Skill 文件播种到一个新 Sandbox。"""
    baseline = sync_workspace_into_sandbox(
        backend=backend,
        workspace=workspace,
    )
    backend.set_workspace_baseline(baseline)
    if not skill_files:
        return
    skill_responses = backend.upload_files(skill_files)
    raise_for_file_errors(skill_responses, operation="upload Sandbox Skill files")
    if protect_skills:
        make_skill_projection_read_only(backend)


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
    transfer_errors: list[str] = []
    for response in responses:
        if response.error:
            transfer_errors.append(f"{response.path}: {response.error}")
            continue
        if response.content is None:
            transfer_errors.append(f"{response.path}: empty response")
            continue
        path = _normalize_sandbox_workspace_path(response.path)
        if path is not None:
            current_files[path] = response.content
    missing_paths = sorted(set(unique_paths) - set(current_files))
    if missing_paths:
        transfer_errors.extend(f"{path}: response missing" for path in missing_paths)
    if transfer_errors:
        raise SandboxProviderError(
            "failed to read final Sandbox Workspace: "
            + "; ".join(transfer_errors[:5])
        )

    changed_files = [
        (path, content)
        for path, content in current_files.items()
        if baseline_manifest.get(path) != sha256(content).hexdigest()
    ]
    upload_responses = workspace.upload_files(changed_files)
    raise_for_file_errors(upload_responses, operation="persist Workspace files")

    deleted_paths = sorted(set(baseline_manifest) - set(current_files))
    delete_errors = workspace.delete_files(deleted_paths)
    if delete_errors:
        raise SandboxProviderError(
            "failed to delete Workspace files: "
            + "; ".join(delete_errors[:5])
        )


def sync_workspace_into_sandbox(
    *,
    backend: SandboxBackendProtocol,
    workspace: AgentWorkspaceBackend,
) -> dict[str, str]:
    """把权威 Workspace 文件播种到 Sandbox，并返回初始摘要。"""
    manifest = workspace.content_manifest()
    result = workspace.ls("/")
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
    raise_for_file_errors(responses, operation="read existing Workspace files")
    files = [
        (_workspace_sandbox_path(response.path), response.content)
        for response in responses
        if response.content is not None and not response.error
    ]
    if len(files) != len(paths):
        raise SandboxProviderError(
            "failed to read existing Workspace files: response content missing"
        )
    upload_responses = backend.upload_files(files)
    raise_for_file_errors(upload_responses, operation="upload Sandbox files")
    return manifest


def _workspace_sandbox_path(path: str) -> str:
    """把 Workspace Backend 的路由相对路径转换为 Sandbox 路径。"""
    normalized = "/" + path.lstrip("/")
    return "/workspace" if normalized == "/" else f"/workspace{normalized}"


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


def raise_for_file_errors(
    responses: Iterable[object],
    *,
    operation: str,
) -> None:
    """把批量文件传输的逐项错误转换为包含路径的同步异常。"""
    errors = [
        f"{getattr(response, 'path', '<unknown>')}: {getattr(response, 'error', '')}"
        for response in responses
        if getattr(response, "error", None)
    ]
    if errors:
        raise SandboxProviderError(f"{operation} failed: {'; '.join(errors[:5])}")


__all__ = [
    "close_workspace_safely",
    "ensure_sandbox_workspace",
    "make_skill_projection_read_only",
    "raise_for_file_errors",
    "seed_sandbox_workspace",
    "SandboxProviderError",
    "sync_workspace_changes",
    "sync_workspace_into_sandbox",
]
