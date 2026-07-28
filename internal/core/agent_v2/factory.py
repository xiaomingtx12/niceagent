"""MyDify 的 DeepAgents graph 装配入口。"""

from __future__ import annotations

from typing import Any, NotRequired, Sequence

from deepagents import create_deep_agent
from deepagents.backends.protocol import BackendProtocol
from deepagents.graph import DeepAgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver

from internal.core.agent_v2.middleware.model_compat_middleware import (
    ModelCompatMiddleware,
)
from internal.core.agent_v2.middleware.project_context_middleware import (
    ProjectContextMiddleware,
)
from internal.core.agent_v2.middleware.review_middleware import ReviewMiddleware
from internal.core.agent_v2.middleware.summarization_middleware import (
    build_summarization_middleware,
)
from internal.core.agent_v2.saver import get_postgres_saver
from internal.core.agent_v2.subagent_specs import build_subagent_specs


class MyDifyDeepAgentState(DeepAgentState):
    """扩展 DeepAgentState 加 R0 需要的运行时字段。

    字段约定（与 ProjectContextMiddleware / ReviewMiddleware 写入路径对应）：
    - runtime_account_id / runtime_app_id / runtime_conversation_id: 由
      ProjectContextMiddleware.before_agent 注入
    - runtime_review_blocked / runtime_review_hit_keyword / runtime_review_preset_response:
      由 ReviewMiddleware.before_model 写入，BaseAgent 兜底处理
    """

    runtime_account_id: NotRequired[str]
    runtime_app_id: NotRequired[str]
    runtime_conversation_id: NotRequired[str]
    runtime_invoked_at: NotRequired[str]
    runtime_recalled_facts: NotRequired[list[dict[str, Any]]]
    runtime_review_blocked: NotRequired[bool]
    runtime_review_hit_keyword: NotRequired[str]
    runtime_review_preset_response: NotRequired[str]


def build_deep_agent(
    *,
    llm: BaseChatModel,
    tools: Sequence[BaseTool],
    system_prompt: str,
    review_config: dict,
    account_id: str,
    app_id: str,
    conversation_id: str,
    checkpointer: BaseCheckpointSaver | bool | None = None,
    use_checkpointer: bool | None = None,
    workspace_backend: BackendProtocol | None = None,
    max_message_count: int = 20,
    subagents: dict[str, dict] | None = None,
) -> Any:
    """构造 deepagents `CompiledStateGraph`。

    Args:
        llm: 已包好的 BaseChatModel（master 老 BaseLanguageModel 不直接支持，
            调用方负责先 `langchain.chat_models.init_chat_model(...)` 包装）
        tools: 该 agent 可用的工具列表（master 老 LangChain Tool）
        system_prompt: 渲染好的 system_prompt（含 L1 业务身份 + preset_prompt）
        review_config: 来自 AppConfigVersion.config.review_config
        account_id / app_id / conversation_id: 用于 L1 业务身份注入 + thread_id
        checkpointer: LangGraph checkpointer；默认不启用。传 ``True`` 可使用
            LangGraph 的内存 checkpointer，传 saver 实例可启用持久化。
        use_checkpointer: 兼容旧调用。``True`` 延迟创建 PostgresSaver，
            ``False`` 显式关闭；新调用应优先传 ``checkpointer``。
        workspace_backend: 可选 DeepAgents backend。通过框架原生 ``backend=``
            路由给主 Agent、同步子 Agent、文件工具与摘要器。
        max_message_count: 大于 0 时用业务阈值替换框架默认摘要器；否则保留
            DeepAgents 默认摘要策略。
        subagents: R3 业务 sub-agent 配置 (AgentConfig.subagents dict);
            有 graph_id 的条目走 AsyncSubAgentMiddleware (远端异步),
            其余走 SubAgentMiddleware (同步 task 工具); None / 空 dict 不挂

    Returns:
        CompiledStateGraph，可直接 `agent.invoke(input, config)` 或 `agent.stream(...)`
    """
    middleware = [
        # 事件直接从 LangGraph updates 翻译；middleware 只保留真正参与图执行
        # 的业务逻辑。
        ProjectContextMiddleware(
            account_id=account_id,
            app_id=app_id,
            conversation_id=conversation_id,
        ),
        ReviewMiddleware(review_config=review_config or {}),
        ModelCompatMiddleware(expected_chat_model=llm),
    ]

    # DeepAgents 会按 middleware.name 原位替换 core middleware。自定义摘要器
    # 使用原生名称，因此不会生成第二个 SummarizationMiddleware。
    summarization_mw = build_summarization_middleware(
        llm=llm,
        max_message_count=max_message_count,
        backend=workspace_backend,  # offload 超长 tool_call 内容
    )
    if summarization_mw is not None:
        middleware.append(summarization_mw)

    if use_checkpointer is not None:
        if checkpointer is not None:
            raise ValueError(
                "checkpointer and use_checkpointer cannot be provided together"
            )
        checkpointer = get_postgres_saver() if use_checkpointer else False

    # R3: 业务 subagents dict → deepagents SubAgent / AsyncSubAgent spec 列表.
    # deepagents 靠 "graph_id" in spec 路由: 有 → AsyncSubAgentMiddleware,
    # 无 → SubAgentMiddleware (task 工具). 上下文分离是 deepagents 原生行为
    # (task 调 sub-agent 时排除 messages/todos, 只带 description).
    subagent_specs = build_subagent_specs(subagents)

    return create_deep_agent(
        model=llm,
        tools=list(tools),
        system_prompt=system_prompt,
        middleware=middleware,
        subagents=subagent_specs or None,
        backend=workspace_backend,
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
