"""Sandbox 临时工作区与权威 Workspace 之间的同步逻辑。"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any

from deepagents.backends.protocol import SandboxBackendProtocol

from internal.core.agent_v2.observability.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)
from internal.core.agent_v2.backends.workspace.workspace_backend import WorkspaceBackend
from internal.core.agent_v2.entity.workspace_snapshot import WorkspaceManifest


class SandboxProviderError(RuntimeError):
    """Sandbox provider 创建、文件同步或清理失败。"""


def close_workspace_safely(
    workspace: WorkspaceBackend,
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
        detail = str(getattr(result, "output", "") or "").strip()
        message = "failed to create Sandbox Workspace directory"
        if detail:
            message = f"{message}: {detail[:500]}"
        raise SandboxProviderError(message)


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
    workspace: WorkspaceBackend,
    skill_files: list[tuple[str, bytes]],
    protect_skills: bool = False,
    skill_uploader: Callable[[list[tuple[str, bytes]]], None] | None = None,
) -> None:
    """把权威 Workspace 和 Skill 文件播种到一个新 Sandbox。"""
    baseline = sync_workspace_into_sandbox(
        backend=backend,
        workspace=workspace,
    )
    if skill_files:
        if skill_uploader is not None:
            skill_uploader(skill_files)
        else:
            skill_responses = backend.upload_files(skill_files)
            raise_for_file_errors(skill_responses, operation="upload Sandbox Skill files")
        if protect_skills:
            make_skill_projection_read_only(backend)
    backend.set_workspace_baseline(baseline)


def sync_workspace_changes(
    *,
    backend: SandboxBackendProtocol,
    workspace: WorkspaceBackend,
    baseline_manifest: WorkspaceManifest,
) -> None:
    """按摘要增量同步 Sandbox 产物，处理新增、变更和删除。"""
    current_files = _read_sandbox_files(backend)
    changed_files = [
        (path, content)
        for path, content in current_files.items()
        if path not in baseline_manifest
        or baseline_manifest[path].content_sha256 != sha256(content).hexdigest()
    ]
    expected_revisions = {
        path: baseline_manifest[path].revision if path in baseline_manifest else 0
        for path, _ in changed_files
    }
    upload_responses = workspace.upload_files(
        changed_files, expected_revisions=expected_revisions
    )
    raise_for_file_errors(upload_responses, operation="persist Workspace files")

    deleted_paths = sorted(set(baseline_manifest) - set(current_files))
    delete_errors = workspace.delete_files(
        deleted_paths,
        expected_revisions={
            path: baseline_manifest[path].revision for path in deleted_paths
        },
    )
    if delete_errors:
        raise SandboxProviderError(
            "failed to delete Workspace files: "
            + "; ".join(delete_errors[:5])
        )


def _read_sandbox_files(backend: SandboxBackendProtocol) -> dict[str, bytes]:
    """读取并规范化 Sandbox 当前文件，集中处理 Provider 返回差异。"""
    result = backend.glob("**/*", path="/workspace")
    if result.error:
        raise SandboxProviderError(result.error)
    paths: list[str] = []
    for item in result.matches or []:
        if item.get("is_dir"):
            continue
        path = _normalize_sandbox_workspace_path(item.get("path"))
        if path is not None:
            paths.append(path)
    paths = sorted(set(paths))
    responses = backend.download_files(paths)
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
    missing_paths = sorted(set(paths) - set(current_files))
    if missing_paths:
        transfer_errors.extend(f"{path}: response missing" for path in missing_paths)
    if transfer_errors:
        raise SandboxProviderError(
            "failed to read final Sandbox Workspace: "
            + "; ".join(transfer_errors[:5])
        )
    return current_files


def sync_workspace_into_sandbox(
    *,
    backend: SandboxBackendProtocol,
    workspace: WorkspaceBackend,
) -> WorkspaceManifest:
    """使用同一份内容快照播种 Sandbox，并保留每个文件的写入前置版本。"""
    manifest, files = workspace.export_snapshot()
    if not files:
        return manifest
    upload_responses = backend.upload_files(files)
    raise_for_file_errors(upload_responses, operation="upload Sandbox files")
    if len(upload_responses) != len(files):
        raise SandboxProviderError("failed to upload Sandbox files: response missing")
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
