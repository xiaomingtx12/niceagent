"""Agent V2 业务调用层的延迟导出。"""

from importlib import import_module

_EXPORTS = {
    "AgentConfig": ("internal.core.agent_v2.agents.models", "AgentConfig"),
    "AgentResult": ("internal.core.agent_v2.agents.models", "AgentResult"),
    "AgentThought": ("internal.core.agent_v2.agents.models", "AgentThought"),
    "AgentTodo": ("internal.core.agent_v2.agents.models", "AgentTodo"),
    "AgentType": ("internal.core.agent_v2.agents.models", "AgentType"),
    "DATASET_RETRIEVAL_TOOL_NAME": (
        "internal.core.agent_v2.agents.models",
        "DATASET_RETRIEVAL_TOOL_NAME",
    ),
    "QueueEvent": ("internal.core.agent_v2.agents.models", "QueueEvent"),
    "AgentQueueManager": (
        "internal.core.agent_v2.agents.agent_queue_manager",
        "AgentQueueManager",
    ),
    "BaseAgent": ("internal.core.agent_v2.agents.base_agent", "BaseAgent"),
}


def __getattr__(name: str):
    """按需加载 Agent 对象，避免事件模块反向导入执行器。"""
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    return getattr(import_module(module_name), attribute)


__all__ = list(_EXPORTS)
