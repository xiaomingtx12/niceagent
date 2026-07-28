"""R0 deepagents 集成入口。

导出：
- `build_deep_agent`: 工厂方法，组装 middleware + checkpointer → CompiledStateGraph
- `MyDifyDeepAgentState`: 扩展 DeepAgentState 的 state schema
- `build_thread_config`: 构造 LangGraph RunnableConfig
- 4 个中间件 + saver + context_window_calculator：上层按需引用

master 老路径（`internal/core/agent/agents/base_agent.py::_build_agent`、
`function_call_agent.py`、`react_agent.py`）R0 阶段不删除——它们会被
BaseAgent 改成调 `build_deep_agent`，但代码还在以便逐步切换。

约束（硬约束 #5）：只暴露 deepagents / langchain 原生符号 + 4 个
业务中间件，不暴露 master 老的 FunctionCallAgent/ReACTAgent 符号。
"""

from internal.core.agent_v2.factory import (
    MyDifyDeepAgentState,
    build_deep_agent,
    build_thread_config,
)
from internal.core.agent_v2.middleware.event_translation_middleware import (
    translate_ai_message,
    translate_tool_message,
)
from internal.core.agent_v2.middleware.model_compat_middleware import (
    ModelCompatMiddleware,
    normalize_usage,
)
from internal.core.agent_v2.middleware.project_context_middleware import (
    ProjectContextMiddleware,
)
from internal.core.agent_v2.middleware.review_middleware import (
    ReviewBlocked,
    ReviewMiddleware,
    apply_keyword_filter,
)
from internal.core.agent_v2.memory.context_window_calculator import (
    TokenEstimate,
    estimate_message_tokens,
    model_context_window,
    should_trigger_summarization,
    summarization_threshold,
)
from internal.core.agent_v2.saver import (
    PostgresSaver,
    build_thread_id,
    get_postgres_saver,
)


__all__ = [
    "ModelCompatMiddleware",
    "MyDifyDeepAgentState",
    "ProjectContextMiddleware",
    "ReviewBlocked",
    "ReviewMiddleware",
    "TokenEstimate",
    "apply_keyword_filter",
    "build_deep_agent",
    "build_thread_config",
    "build_thread_id",
    "estimate_message_tokens",
    "get_postgres_saver",
    "model_context_window",
    "normalize_usage",
    "should_trigger_summarization",
    "summarization_threshold",
    "translate_ai_message",
    "translate_tool_message",
]
