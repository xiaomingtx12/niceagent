"""Agent V2 的 DeepAgents 文件记忆装配。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from langgraph.store.base import BaseStore

from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.backends.memory.agent_memory_backend import AgentMemoryBackend
from internal.core.agent_v2.entity.agent_memory import AGENT_MEMORY_INDEX


@dataclass
class MemoryProjection:
    """当前 Agent 可见的持久化文件记忆路由。"""

    routes: dict[str, BackendProtocol] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    store: BaseStore | None = None


def build_memory_context(*, enabled: bool, store_provider: Callable[[], BaseStore] | None = None) -> MemoryProjection:
    """按应用开关构造租户隔离的长期记忆路由。"""
    # 1.关闭时不初始化 Store，也不向 Agent 暴露记忆路由。
    if not enabled:
        return MemoryProjection()

    # 2.获取平台配置的 Store，缺失时直接拒绝启动。
    store = store_provider() if store_provider else None
    if store is None:
        raise RuntimeError(
            "长期记忆 Store 未配置，Agent 运行时拒绝在无记忆模式下启动"
        )
    # 3.复用官方 StoreBackend，并补充文本和容量边界。
    memory_backend: BackendProtocol = AgentMemoryBackend(
        store=store,
        namespace=memory_namespace_from_runtime,
    )
    # 4.CompositeBackend 负责把 /memories/ 转换为 Store 相对 key。
    return MemoryProjection(
        routes={"/memories/": memory_backend},
        sources=[AGENT_MEMORY_INDEX],
        store=store,
    )



def memory_namespace_from_runtime(runtime: Any) -> tuple[str, ...]:
    """从单次 LangGraph runtime context 解析 Store 命名空间。"""
    context = getattr(runtime, "context", None)
    if not isinstance(context, AgentRunContext):
        raise RuntimeError("AgentRunContext is required for memory Store access")
    return context.memory_namespace


__all__ = [
    "build_memory_context",
    "MemoryProjection",
    "memory_namespace_from_runtime",
]
