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

from internal.core.agent_v2.observability.telemetry import AgentTelemetry
from internal.core.agent_v2.backends.sandbox.lazy_sandbox_backend import LazySandboxBackend
from internal.core.agent_v2.backends.sandbox.sandbox_workspace_sync import (
    SandboxProviderError,
    close_workspace_safely,
)
from internal.core.agent_v2.backends.workspace.workspace_backend import WorkspaceBackend
from internal.extension.logging_extension import logger


_DISABLED_PROVIDERS = {"disabled", "none", "off", "false"}
_PROVIDER_ALIASES = {"bwrap": "bubblewrap"}
_ProviderCreator = Callable[..., SandboxBackendProtocol]


def build_sandbox_backend(
    *,
    settings: Any,
    workspace: WorkspaceBackend,
    sandbox_name: str,
    skill_files: Iterable[tuple[str, bytes]],
    telemetry: AgentTelemetry | None = None,
) -> SandboxBackendProtocol | None:
    """按配置选择 Provider，远程资源延迟到首次需要 Sandbox 时初始化。

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
    files = list(skill_files)

    def create() -> SandboxBackendProtocol:
        """SDK 加载前由工厂持有 Workspace，交给 Provider 后沿用其失败清理。"""
        try:
            creator = _load_provider_creator(canonical_provider, module_path)
        except Exception:
            close_workspace_safely(workspace, telemetry=telemetry)
            raise
        try:
            return creator(
                settings=settings,
                workspace=workspace,
                sandbox_name=sandbox_name,
                skill_files=files,
                telemetry=telemetry,
            )
        except SandboxProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SandboxProviderError(
                f"failed to initialize {canonical_provider} Sandbox"
            ) from exc

    if canonical_provider in {"daytona", "langsmith"}:
        return LazySandboxBackend(
            creator=create, workspace=workspace, sandbox_name=sandbox_name,
            provider=canonical_provider, telemetry=telemetry,
        )
    return create()


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


def delete_agent_sandbox(
    *,
    settings: Any,
    account_id: object,
    app_id: object,
    conversation_id: object,
) -> None:
    """清理会话持有的远程资源；保持与正常运行结束时关闭 Backend 相区分。"""
    provider = _normalize_provider(settings)
    if provider not in {"langsmith", "daytona"}:
        return
    name = build_stable_sandbox_name(
        account_id=account_id,
        app_id=app_id,
        conversation_id=conversation_id,
        agent_id="main",
    )
    try:
        module = import_module(_PROVIDER_MODULES[provider])
        module.delete_sandbox(name)
    except Exception:
        # 远程删除失败不回滚已提交的业务删除，保留上下文供排查与补偿。
        logger.warning(
            "删除 Agent Sandbox 失败: account_id=%s app_id=%s "
            "conversation_id=%s sandbox=%s",
            account_id,
            app_id,
            conversation_id,
            name,
            exc_info=True,
        )


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


__all__ = ["build_sandbox_backend", "build_stable_sandbox_name", "delete_agent_sandbox"]
