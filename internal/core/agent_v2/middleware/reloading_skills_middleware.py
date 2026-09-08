"""按当前 Run 的授权范围刷新 checkpoint 中缓存的技能目录。"""

from typing import Any

from deepagents.middleware.skills import SkillsMiddleware, SkillsState, SkillsStateUpdate
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime


class ReloadingSkillsMiddleware(SkillsMiddleware):
    """仅刷新技能元数据，提示词仍由官方 SkillsMiddleware 注入。"""

    def before_agent(
        self, state: SkillsState, runtime: Runtime[Any], config: RunnableConfig
    ) -> SkillsStateUpdate:
        """每轮重新枚举授权投影，避免上一轮选择或版本残留。"""
        fresh_state = dict(state)
        fresh_state.pop("skills_metadata", None)
        fresh_state.pop("skills_load_errors", None)
        return {"skills_load_errors": [], **(super().before_agent(fresh_state, runtime, config) or {})}

    async def abefore_agent(
        self, state: SkillsState, runtime: Runtime[Any], config: RunnableConfig
    ) -> SkillsStateUpdate:
        """异步运行同样不能复用上一轮的授权列表。"""
        fresh_state = dict(state)
        fresh_state.pop("skills_metadata", None)
        fresh_state.pop("skills_load_errors", None)
        return {"skills_load_errors": [], **(await super().abefore_agent(fresh_state, runtime, config) or {})}
