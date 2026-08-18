"""稳定的 LangGraph 线程配置合并。"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig

from internal.core.agent_v2.runtime.models import AgentRunContext


def merge_thread_config(
    runtime_context: AgentRunContext,
    config: RunnableConfig | None,
) -> RunnableConfig | None:
    """合并调用参数并锁定租户线程标识。"""
    defaults = runtime_context.thread.to_config()
    if config is None:
        return defaults

    merged: RunnableConfig = dict(defaults)
    merged.update(config)
    merged["configurable"] = {
        **(config.get("configurable") or {}),
        **defaults["configurable"],
    }
    merged["metadata"] = {
        **(config.get("metadata") or {}),
        **defaults["metadata"],
    }
    return merged


__all__ = ["merge_thread_config"]
