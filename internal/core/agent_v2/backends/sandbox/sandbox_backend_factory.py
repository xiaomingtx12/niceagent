"""DeepAgents Sandbox Provider 的唯一公开装配入口。

每个 Provider 模块只实现统一签名的 ``create_backend`` 内部契约；模块本身
不向包级别导出构造函数，避免调用方绕过 Provider 选择和统一清理流程。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from hashlib import sha256
from importlib import import_module
from typing import Any

from deepagents.backends.protocol import SandboxBackendProtocol

from internal.core.agent_v2.assembly.observability.telemetry import AgentTelemetry
from internal.core.agent_v2.backends.sandbox.sandbox_workspace_sync import (
    SandboxProviderError,
    close_workspace_safely,
)
from internal.core.agent_v2.backends.workspace.workspace_backend import AgentWorkspaceBackend


_DISABLED_PROVIDERS = {"disabled", "none", "off", "false"}
_PROVIDER_ALIASES = {"bwrap": "bubblewrap"}
_ProviderCreator = Callable[..., SandboxBackendProtocol]


def build_sandbox_backend(
    *,
    settings: Any,
    workspace: AgentWorkspaceBackend,
    sandbox_name: str,
    skill_files: Iterable[tuple[str, bytes]],
    telemetry: AgentTelemetry | None = None,
) -> SandboxBackendProtocol | None:
    """按配置选择一个 Provider，并交给其实现完成初始化。

    入口只处理规范化、禁用值和 Provider 分发。每个 Provider 的可选依赖、
    Workspace 播种和失败清理都隐藏在对应模块中。
    """
    provider = _normalize_provider(settings)
    if provider in _DISABLED_PROVIDERS:
        return None

    canonical_provider = _PROVIDER_ALIASES.get(provider, provider)
    module_path = _PROVIDER_MODULES.get(canonical_provider)
    if module_path is None:
        close_workspace_safely(workspace, telemetry=telemetry)
        raise SandboxProviderError(
            f"unsupported AGENT_SANDBOX_PROVIDER: {provider}"
        )
    try:
        creator = _load_provider_creator(canonical_provider, module_path)
        return creator(
            settings=settings,
            workspace=workspace,
            sandbox_name=sandbox_name,
            skill_files=list(skill_files),
            telemetry=telemetry,
        )
    except SandboxProviderError:
        close_workspace_safely(workspace, telemetry=telemetry)
        raise
    except Exception as exc:  # noqa: BLE001
        close_workspace_safely(workspace, telemetry=telemetry)
        raise SandboxProviderError(
            f"failed to initialize {canonical_provider} Sandbox"
        ) from exc


def _load_provider_creator(
    provider: str,
    module_path: str,
) -> _ProviderCreator:
    """懒加载 Provider 模块，避免可选 SDK 影响其他 Provider。"""
    try:
        module = import_module(module_path)
    except ImportError as exc:
        if provider == "daytona":
            raise SandboxProviderError(
                "Daytona provider requires langchain-daytona and daytona"
            ) from exc
        raise

    creator = getattr(module, "create_backend", None)
    if not callable(creator):
        raise SandboxProviderError(
            f"Sandbox provider module {module_path} does not expose create_backend"
        )
    return creator


def _normalize_provider(settings: Any) -> str:
    """读取并规范 Provider 名称，避免分支分别处理大小写和空白。"""
    return str(
        getattr(settings, "AGENT_SANDBOX_PROVIDER", "disabled")
    ).strip().lower()


_PROVIDER_MODULES: dict[str, str] = {
    "bubblewrap": (
        "internal.core.agent_v2.backends.sandbox.bubblewrap_sandbox_backend"
    ),
    "local_shell": "internal.core.agent_v2.backends.sandbox.local_shell_backend",
    "daytona": "internal.core.agent_v2.backends.sandbox.daytona_sandbox_backend",
    "langsmith": "internal.core.agent_v2.backends.sandbox.langsmith_sandbox_backend",
}


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


__all__ = ["build_sandbox_backend", "build_stable_sandbox_name"]
