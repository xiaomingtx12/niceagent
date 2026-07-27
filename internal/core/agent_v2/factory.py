"""R0 build_deep_agent factory。

单一入口：所有 agent 路径（chat / debug_chat / agent_loop / app_handler）
都通过这个函数构造 deepagents `CompiledStateGraph`。

约束（硬约束 #5）：只用 deepagents / langchain 提供的机制：
- 不再 `function_call_agent.py` / `react_agent.py` 手写 StateGraph 节点
- 不再造 master 老的 `BaseAgent._build_agent()` 抽象
- 持久化走 LangGraph 原生 `PostgresSaver`（不在 my-dify 自己的表）
- 审核 / L1 业务身份 / 事件翻译走自定义 4 个中间件
- R1 文件系统走 MyDifyWorkspaceBackend（DB 持久化），不挂载 LocalBackend
"""

from __future__ import annotations

from typing import Any, Optional, Sequence
from uuid import UUID, uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph import MessagesState

from deepagents import create_deep_agent
from deepagents.graph import DeepAgentState
from deepagents.middleware import FilesystemMiddleware

from internal.core.agent_v2.middleware.event_translation_middleware import (
    EventTranslationMiddleware,
)
from internal.core.agent_v2.middleware.model_compat_middleware import (
    ModelCompatMiddleware,
)
from internal.core.agent_v2.middleware.project_context_middleware import (
    ProjectContextMiddleware,
)
from internal.core.agent_v2.middleware.review_middleware import ReviewMiddleware
from internal.core.agent_v2.saver import get_postgres_saver
from internal.core.agent_v2.workspace_backend import MyDifyWorkspaceBackend


class MyDifyDeepAgentState(DeepAgentState):
    """扩展 DeepAgentState 加 R0 需要的运行时字段。

    字段约定（与 EventTranslationMiddleware / ProjectContextMiddleware 写入
    路径一一对应）：
    - task_id: 每次 invoke 由 EventTranslationMiddleware.before_agent 注入
    - agent_thoughts: 累计 AgentThought，供 stream 端点读
    - runtime_account_id / runtime_app_id / runtime_conversation_id: 由
      ProjectContextMiddleware.before_agent 注入
    - runtime_review_blocked / runtime_review_hit_keyword / runtime_review_preset_response:
      由 ReviewMiddleware.before_model 写入，BaseAgent 兜底处理
    """

    # task_id: UUID  # LangGraph MessagesState 风格的 TypedDict 不能直接给类型注解；
    # 实际就是 dict[str, Any]，deepagents 把 state 当 dict 用；下面注释即可。
    # agent_thoughts: list[dict]
    # runtime_account_id: str
    # runtime_app_id: str
    # runtime_conversation_id: str
    # runtime_review_blocked: bool
    # runtime_review_hit_keyword: str
    # runtime_review_preset_response: str
    pass


def build_deep_agent(
    *,
    llm: BaseChatModel,
    tools: Sequence[BaseTool],
    system_prompt: str,
    review_config: dict,
    account_id: str,
    app_id: str,
    conversation_id: str,
    task_id: Optional[UUID] = None,
    use_checkpointer: bool = True,
    workspace_backend: Optional[MyDifyWorkspaceBackend] = None,
) -> Any:
    """构造 deepagents `CompiledStateGraph`。

    Args:
        llm: 已包好的 BaseChatModel（master 老 BaseLanguageModel 不直接支持，
            调用方负责先 `langchain.chat_models.init_chat_model(...)` 包装）
        tools: 该 agent 可用的工具列表（master 老 LangChain Tool）
        system_prompt: 渲染好的 system_prompt（含 L1 业务身份 + preset_prompt）
        review_config: 来自 AppConfigVersion.config.review_config
        account_id / app_id / conversation_id: 用于 L1 业务身份注入 + thread_id
        task_id: 透传到 EventTranslationMiddleware；不传则自动生成
        use_checkpointer: 默认 True（走 PostgresSaver）；False 用于单测
            （避免每个测试都连 DB）
        workspace_backend: R1 可选 MyDifyWorkspaceBackend 实例；不传时不挂
            FilesystemMiddleware（agent 没有 ls / read_file / write_file 工具）

    Returns:
        CompiledStateGraph，可直接 `agent.invoke(input, config)` 或 `agent.stream(...)`
    """
    effective_task_id = task_id or uuid4()

    middleware = [
        # 顺序：ProjectContext (L1) → Review (input 拦截) → ModelCompat (字段归一)
        # → EventTranslation (事件翻译)；EventTranslation 必须最后，因为
        # wrap_model_call 会拦截 LLM 输出；ModelCompat 紧贴 LLM 把 token_usage
        # 归一化成 usage_metadata 给 EventTranslation 用。
        ProjectContextMiddleware(
            account_id=account_id,
            app_id=app_id,
            conversation_id=conversation_id,
        ),
        ReviewMiddleware(review_config=review_config or {}),
        ModelCompatMiddleware(expected_chat_model=llm),
        EventTranslationMiddleware(task_id=effective_task_id),
    ]

    # R1.4: FilesystemMiddleware 接 DB 持久化 backend；放最前——让 agent
    # system prompt 早拿到 workspace 路径指引（ProjectContextMiddleware 也
    # 会注入，但 FilesystemMiddleware 自身会追加 system 提示）
    if workspace_backend is not None:
        middleware.append(
            FilesystemMiddleware(backend=workspace_backend)
        )

    checkpointer = get_postgres_saver() if use_checkpointer else False

    return create_deep_agent(
        model=llm,
        tools=list(tools),
        system_prompt=system_prompt,
        middleware=middleware,
        checkpointer=checkpointer,
        state_schema=MyDifyDeepAgentState,
    )


def build_thread_config(
    *,
    account_id: str,
    app_id: str,
    conversation_id: str,
    agent_id: str = "main",
) -> dict:
    """构造 LangGraph RunnableConfig（thread_id + 用户元数据）。"""
    from internal.core.agent_v2.saver import build_thread_id

    return {
        "configurable": {
            "thread_id": build_thread_id(
                account_id=account_id,
                app_id=app_id,
                conversation_id=conversation_id,
                agent_id=agent_id,
            ),
        },
        "metadata": {
            "account_id": account_id,
            "app_id": app_id,
            "conversation_id": conversation_id,
            "agent_id": agent_id,
        },
    }


__all__ = [
    "MyDifyDeepAgentState",
    "build_deep_agent",
    "build_thread_config",
]