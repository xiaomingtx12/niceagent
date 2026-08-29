"""Agent 单次执行的回调配置与 Backend 资源释放。"""

from __future__ import annotations

from deepagents.backends.protocol import BackendProtocol
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.runnables import RunnableConfig


def append_usage_callback(
    config: RunnableConfig | None,
    callback: UsageMetadataCallbackHandler,
) -> RunnableConfig:
    """附加当前运行专用的用量聚合器，不修改调用方配置。"""
    effective: RunnableConfig = dict(config or {})
    existing = effective.get("callbacks")
    if existing is None:
        callbacks = []
    elif isinstance(existing, list):
        callbacks = list(existing)
    else:
        callbacks = [existing]
    callbacks.append(callback)
    effective["callbacks"] = callbacks
    return effective


def close_runtime_backends(backend: BackendProtocol | None) -> None:
    """逐个关闭运行时 Backend，避免共享实例被重复关闭。"""
    if backend is None:
        return
    candidates = [backend, getattr(backend, "default", None)]
    routes = getattr(backend, "routes", {}) or {}
    candidates.extend(routes.values())
    seen: set[int] = set()
    errors: list[Exception] = []
    for candidate in candidates:
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        close = getattr(candidate, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
    if errors:
        raise RuntimeError(
            f"failed to close {len(errors)} runtime backend(s)"
        ) from errors[0]


__all__ = ["append_usage_callback", "close_runtime_backends"]
