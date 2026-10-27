"""Sandbox 初始化的单向文件传输，不进行持久化同步。"""

from collections.abc import Iterable

from deepagents.backends.protocol import SandboxBackendProtocol

from internal.core.agent_v2.backends.workspace.workspace_projection import WorkspaceProjection


class SandboxProviderError(RuntimeError):
    """Sandbox 创建、文件传输或资源清理失败。"""


def seed_sandbox_workspace(
    backend: SandboxBackendProtocol, *, workspace: WorkspaceProjection,
) -> None:
    """远程启动时只迁移本轮工作副本，已保存文件必须由交付入口按需加载。"""
    files = workspace.files()
    if not files:
        return
    responses = backend.upload_files(files)
    raise_for_file_errors(responses, operation="initialize Sandbox working copy")
    if len(responses) != len(files):
        raise SandboxProviderError("initialize Sandbox working copy: response missing")


def raise_for_file_errors(responses: Iterable[object], *, operation: str) -> None:
    """把逐文件传输错误统一转为 Provider 异常，不把失败当作初始化成功。"""
    errors = [
        f"{getattr(response, 'path', '<unknown>')}: {getattr(response, 'error', '')}"
        for response in responses if getattr(response, "error", None)
    ]
    if errors:
        raise SandboxProviderError(f"{operation} failed: {'; '.join(errors[:5])}")
