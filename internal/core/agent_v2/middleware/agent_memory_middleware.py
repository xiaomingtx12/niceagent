"""长期记忆删除工具与可信身份校验；固定规则由构图阶段统一装配。"""

import json

from langchain.agents.middleware.types import AgentMiddleware
from langchain.tools import ToolRuntime, tool

from internal.core.agent_v2.entity.agent_memory import AgentMemoryIdentity, normalize_agent_memory_path
from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.execution.memory_updates import publish_memory_update
from internal.core.agent_v2.persistence.agent_memory_repository import (
    AgentMemoryRepository,
)


def memory_identity(runtime) -> AgentMemoryIdentity:
    """即使恢复旧 checkpoint，也只能使用本轮服务端授权的记忆身份。"""
    context = runtime.context
    if (
        not isinstance(context, AgentRunContext)
        or not context.memory_enabled
        or runtime.store is None
        or context.agent_id != "main"
    ):
        raise ValueError("当前 Agent 未启用长期记忆")
    return AgentMemoryIdentity(account_id=context.account_id, app_id=context.app_id)


class AgentMemoryMiddleware(AgentMiddleware):
    """仅提供删除工具；文件增改查由原生工具处理，提示词由装配层提供。"""

    def __init__(self):
        """文件增改复用原生工具，仅提供框架尚未提供的记忆删除操作。"""
        super().__init__()

        @tool
        def delete_memory_file(path: str, runtime: ToolRuntime[AgentRunContext]) -> str:
            """永久删除指定 /memories/ 文件；先读取确认目标，没有暂停或恢复操作。"""
            try:
                identity = memory_identity(runtime)
                path = normalize_agent_memory_path(path)
                deleted = AgentMemoryRepository(lambda: runtime.store).delete_file(
                    identity, path
                )
                if deleted:
                    publish_memory_update(path=path, action="delete")
                return json.dumps(
                    {
                        "path": path,
                        "deleted": deleted,
                    },
                    ensure_ascii=False,
                )
            except ValueError as exc:
                return json.dumps({"error": str(exc)}, ensure_ascii=False)

        self.tools = [delete_memory_file]
