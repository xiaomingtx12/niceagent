"""DeepAgents Harness Profile adapter for platform-safe defaults."""

from __future__ import annotations

from collections.abc import Mapping
from threading import Lock

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    register_harness_profile,
)
from langchain_core.language_models import BaseChatModel


class HarnessProfileError(RuntimeError):
    """Raised when platform safety settings cannot be bound to a model."""


_REGISTERED_KEYS: set[str] = set()
_REGISTER_LOCK = Lock()


def disable_implicit_general_purpose(model: BaseChatModel) -> str:
    """Disable DeepAgents' broad implicit worker for this concrete model.

    DeepAgents 0.6.12 exposes profile registration publicly, but resolves a
    pre-built model by LangSmith provider metadata plus ``model``/``model_name``.
    This adapter mirrors that documented lookup shape without replacing any
    framework middleware. Failure is fatal because silently retaining the
    default worker would expose an unconfigured, parent-tool-inheriting agent.
    """
    try:
        params = model._get_ls_params()
    except (AttributeError, TypeError, NotImplementedError) as exc:
        raise HarnessProfileError(
            "无法识别模型 provider，不能安全关闭 DeepAgents general-purpose"
        ) from exc
    provider = params.get("ls_provider") if isinstance(params, Mapping) else None
    if not isinstance(provider, str) or not provider or ":" in provider:
        raise HarnessProfileError(
            "无法识别模型 provider，不能安全关闭 DeepAgents general-purpose"
        )

    identifier = getattr(model, "model_name", None) or getattr(model, "model", None)
    if isinstance(identifier, str) and identifier and ":" not in identifier:
        key = f"{provider}:{identifier}"
    elif isinstance(identifier, str) and identifier.count(":") == 1:
        key = identifier
    else:
        key = provider

    with _REGISTER_LOCK:
        if key not in _REGISTERED_KEYS:
            register_harness_profile(
                key,
                HarnessProfile(
                    general_purpose_subagent=GeneralPurposeSubagentProfile(
                        enabled=False
                    )
                ),
            )
            _REGISTERED_KEYS.add(key)
    return key


__all__ = ["HarnessProfileError", "disable_implicit_general_purpose"]

