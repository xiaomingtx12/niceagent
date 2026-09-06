"""稳定的 LangGraph 会话配置合并。"""

from __future__ import annotations

from langchain_core.runnables import RunnableConfig

from internal.core.agent_v2.entity.run_context import AgentRunContext


def merge_conversation_config(
    runtime_context: AgentRunContext,
    config: RunnableConfig | None,
) -> RunnableConfig | None:
    """合并调用参数并锁定服务端会话标识。"""
    # 1.生成由服务端上下文决定的会话配置和元数据。
    defaults = runtime_context.conversation.to_config()
    if config is None:
        return defaults

    # 2.保留调用方的其他 Runnable 配置。
    merged: RunnableConfig = dict(defaults)
    merged.update(config)
    # 3.用可信服务端会话覆盖调用方可能传入的框架标识。
    merged["configurable"] = {
        **(config.get("configurable") or {}),
        **defaults["configurable"],
    }
    merged["metadata"] = {
        **(config.get("metadata") or {}),
        **defaults["metadata"],
    }
    return merged
