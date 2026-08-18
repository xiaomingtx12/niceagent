"""Agent V2 的 DeepAgents 文件记忆装配。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from deepagents.backends import StoreBackend
from deepagents.backends.protocol import BackendProtocol
from langgraph.store.base import BaseStore

from internal.core.agent_v2.runtime.models import AgentRunContext


@dataclass
class MemoryProjection:
    """当前 Agent 可见的持久化文件记忆路由。"""

    routes: dict[str, BackendProtocol] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    store: BaseStore | None = None


class MemoryContextAssembler:
    """把 DeepAgents 原生 StoreBackend 配置为多租户文件记忆。"""

    def __init__(self, store_provider: Any | None = None) -> None:
        self._store_provider = store_provider

    def build(self) -> MemoryProjection:
        """构造租户隔离的 StoreBackend，内容由 Agent 文件工具维护。"""
        store = self._store_provider() if self._store_provider else None
        if store is None:
            return MemoryProjection()
        memory_backend: BackendProtocol = StoreBackend(
            namespace=memory_namespace_from_runtime,
        )
        return MemoryProjection(
            routes={"/memories/": memory_backend},
            sources=["/memories/AGENTS.md"],
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
