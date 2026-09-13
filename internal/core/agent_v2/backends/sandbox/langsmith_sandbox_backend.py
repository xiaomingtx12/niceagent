"""LangSmith Sandbox Backend 的生命周期和初始化适配。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from deepagents.backends import LangSmithSandbox
from deepagents.backends.protocol import SandboxBackendProtocol

from internal.core.agent_v2.backends.sandbox.sandbox_initialization import (
    SandboxProviderError,
    seed_sandbox_workspace,
)
from internal.core.agent_v2.backends.sandbox.sandbox_skill_upload import seed_sandbox_skills
from internal.core.agent_v2.backends.workspace.workspace_projection import WorkspaceProjection
from internal.core.agent_v2.observability.telemetry import AgentTelemetry
from internal.extension.logging_extension import logger

def delete_sandbox(name: str) -> None:
    """删除已知名称的会话 Sandbox；不存在视为清理完成。"""
    from langsmith.sandbox import ResourceNotFoundError, SandboxClient

    try:
        SandboxClient().delete_sandbox(name)
    except ResourceNotFoundError:
        return


def create_backend(
    *,
    settings: Any,
    workspace: WorkspaceProjection,
    sandbox_name: str,
    skill_files: Iterable[tuple[str, bytes]],
    telemetry: AgentTelemetry | None = None,
) -> SandboxBackendProtocol:
    """获取或创建 LangSmith Sandbox，并完成可复用实例的重置。"""
    from langsmith.sandbox import ResourceNotFoundError, SandboxClient

    client = None
    sandbox = None
    created = False
    try:
        # 1.客户端配置失败也进入统一异常边界，Workspace 始终由调用方持有。
        client = SandboxClient()
        try:
            sandbox = client.get_sandbox(sandbox_name)
        except ResourceNotFoundError:
            created = True
            sandbox = client.create_sandbox(
                **_langsmith_create_kwargs(settings, sandbox_name)
            )

        if str(getattr(sandbox, "status", "")).lower() == "stopped":
            sandbox.start()
        # 2.原生 Backend 不需要运行级关闭，会话实例由 TTL 或显式删除管理。
        backend = LangSmithSandbox(sandbox)
        _reset_sandbox_projection(backend)
        seed_sandbox_workspace(backend, workspace=workspace)
        seed_sandbox_skills(backend, list(skill_files), protect=True, compress=True, telemetry=telemetry)
        return backend
    except BaseException as exc:
        # 3.仅删除本次新建但初始化失败的实例，不删除借用的已有会话。
        if created and sandbox is not None:
            try:
                client.delete_sandbox(sandbox.name)
            except ResourceNotFoundError:
                pass
            except Exception:
                logger.exception("LangSmith Sandbox 初始化失败后的远程资源清理失败")
        if not isinstance(exc, Exception):
            raise
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


def _reset_sandbox_projection(backend: LangSmithSandbox) -> None:
    """清空可复用 Sandbox 中上次运行留下的业务投影。"""
    command = (
        "mkdir -p /workspace && "
        "find /workspace -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + && "
        "rm -rf -- /skills /subagent_skills"
    )
    result = backend.execute(command)
    if result.exit_code not in (0, None):
        raise SandboxProviderError("failed to reset Sandbox projections")
