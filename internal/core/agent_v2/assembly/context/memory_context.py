"""Agent V2 的 DeepAgents 文件记忆装配。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from deepagents.backends.protocol import BackendProtocol
from langgraph.store.base import BaseStore

from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.backends.memory.agent_memory_backend import AgentMemoryBackend
from internal.core.agent_v2.entity.agent_memory import AGENT_MEMORY_SOURCE


@dataclass
class MemoryProjection:
    """当前 Agent 可见的持久化文件记忆路由。"""

    routes: dict[str, BackendProtocol] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    store: BaseStore | None = None


class MemoryContextAssembler:
    """把 DeepAgents 原生 StoreBackend 配置为多租户文件记忆。"""

    def __init__(
        self,
        store_provider: Callable[[], BaseStore] | None = None,
    ) -> None:
        """保存 Store 工厂，延迟到运行时创建实际 Store。"""
        self._store_provider = store_provider

    def build(self, *, enabled: bool) -> MemoryProjection:
        """按应用开关构造租户隔离的长期记忆路由。"""
        # 1.关闭时不初始化 Store，也不向 Agent 暴露记忆路由。
        if not enabled:
            return MemoryProjection()

        # 2.获取平台配置的 Store，缺失时直接拒绝启动。
        store = self._store_provider() if self._store_provider else None
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
            sources=[AGENT_MEMORY_SOURCE],
            store=store,
        )


def memory_namespace_from_runtime(runtime: Any) -> tuple[str, ...]:
    """从单次 LangGraph runtime context 解析 Store 命名空间。"""
    context = getattr(runtime, "context", None)
    if not isinstance(context, AgentRunContext):
        raise RuntimeError("AgentRunContext is required for memory Store access")
    return context.memory_namespace


__all__ = [
    "MemoryContextAssembler",
    "MemoryProjection",
    "memory_namespace_from_runtime",
]
