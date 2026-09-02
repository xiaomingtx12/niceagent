"""每次 Agent Run 从长期记忆 Store 重新加载记忆。"""

from __future__ import annotations

from typing import Any

from deepagents.middleware.memory import MemoryMiddleware, MemoryState, MemoryStateUpdate
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime


class ReloadingMemoryMiddleware(MemoryMiddleware):
    """绕过 checkpoint 中的缓存值，只在每个新 Run 开始时刷新一次记忆。"""

    def before_agent(
        self,
        state: MemoryState,
        runtime: Runtime[Any],
        config: RunnableConfig,
    ) -> MemoryStateUpdate | None:
        """删除线程状态中的旧快照后复用官方 Store 加载逻辑。"""
        state_for_reload = dict(state)
        state_for_reload.pop("memory_contents", None)
        return super().before_agent(state_for_reload, runtime, config)

    async def abefore_agent(
        self,
        state: MemoryState,
        runtime: Runtime[Any],
        config: RunnableConfig,
    ) -> MemoryStateUpdate | None:
        """异步版本：每个新 Run 从 Store 重新加载记忆。"""
        state_for_reload = dict(state)
        state_for_reload.pop("memory_contents", None)
        return await super().abefore_agent(state_for_reload, runtime, config)


__all__ = ["ReloadingMemoryMiddleware"]
