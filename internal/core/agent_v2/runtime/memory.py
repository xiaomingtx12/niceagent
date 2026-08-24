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
        """保存 Store 工厂，延迟到运行时创建实际 Store。"""
        self._store_provider = store_provider

    def build(self) -> MemoryProjection:
        """构造租户隔离的 StoreBackend，内容由 Agent 文件工具维护。"""
        # 1.获取平台配置的 Store，缺失时直接拒绝启动。
        store = self._store_provider() if self._store_provider else None
        if store is None:
            raise RuntimeError(
                "长期记忆 Store 未配置，Agent 运行时拒绝在无记忆模式下启动"
            )
        # 2.让 DeepAgents 通过运行时上下文动态解析租户命名空间。
        memory_backend: BackendProtocol = StoreBackend(
            namespace=memory_namespace_from_runtime,
        )
        # 3.只暴露记忆目录和入口文件，不把底层 Store 细节交给 Agent。
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
