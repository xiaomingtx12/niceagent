"""DeepAgents 平台安全能力配置适配器。"""

from __future__ import annotations

from collections.abc import Mapping
from threading import Lock

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)
from langchain_core.language_models import BaseChatModel

from internal.core.agent_v2.middleware.filesystem_input_middleware import (
    FilesystemInputMiddleware,
)


class HarnessProfileError(RuntimeError):
    """模型无法绑定平台安全配置时抛出。"""


_REGISTERED_PROFILES: set[str] = set()
_REGISTER_LOCK = Lock()


def register_platform_harness_profile(
    model: BaseChatModel,
    *,
    allow_execute: bool = False,
) -> str:
    """为具体模型启用 DeepAgents 默认通用 SubAgent。

    `execute` 不在 Harness Profile 中配置。DeepAgents 的
    FilesystemMiddleware 会在每次模型调用前根据解析出的 Backend 是否支持
    SandboxBackendProtocol 动态过滤原生 execute，这样同一模型可以同时服务
    Sandbox 和非 Sandbox 运行，避免全局 Profile 注册状态互相污染。
    """
    try:
        params = model._get_ls_params()
    except (AttributeError, TypeError, NotImplementedError) as exc:
        raise HarnessProfileError(
            "无法识别模型 provider，不能安全配置 DeepAgents general-purpose"
        ) from exc
    provider = params.get("ls_provider") if isinstance(params, Mapping) else None
    if not isinstance(provider, str) or not provider or ":" in provider:
        raise HarnessProfileError(
            "无法识别模型 provider，不能安全配置 DeepAgents general-purpose"
        )

    identifier = getattr(model, "model_name", None) or getattr(model, "model", None)
    if isinstance(identifier, str) and identifier and ":" not in identifier:
        key = f"{provider}:{identifier}"
    elif isinstance(identifier, str) and identifier.count(":") == 1:
        key = identifier
    else:
        key = provider

    with _REGISTER_LOCK:
        if key not in _REGISTERED_PROFILES:
            register_harness_profile(
                key,
                HarnessProfile(
                    # Profile 中的中间件会同时应用到主 Agent 和默认
                    # general-purpose 子 Agent，保持文件工具行为一致。
                    extra_middleware=(FilesystemInputMiddleware(),),
                    general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=True),
                ),
            )
            _REGISTERED_PROFILES.add(key)
    return key


__all__ = [
    "HarnessProfileError",
    "register_platform_harness_profile",
]
