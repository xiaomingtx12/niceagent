"""MyDify 的 DeepAgents graph 装配入口。"""

from __future__ import annotations

from typing import Annotated, Any, NotRequired, Sequence

from deepagents import create_deep_agent
from deepagents.backends.protocol import BackendProtocol
from deepagents.graph import DeepAgentState
from deepagents.middleware.filesystem import FilesystemPermission
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langchain.agents.middleware.types import PrivateStateAttr
from langgraph.checkpoint.base import BaseCheckpointSaver

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
from internal.core.agent_v2.harness_profile import (
    disable_implicit_general_purpose,
)
from internal.core.agent_v2.saver import get_postgres_saver
from internal.core.agent_v2.subagent_specs import (
    build_subagent_specs,
    validate_platform_subagents,
)


class MyDifyDeepAgentState(DeepAgentState):
    """扩展 DeepAgentState 加 R0 需要的运行时字段。

    字段约定（与 ProjectContextMiddleware / ReviewMiddleware 写入路径对应）：
    - runtime_account_id / runtime_app_id / runtime_conversation_id: 由
      ProjectContextMiddleware.before_agent 注入
    - runtime_review_blocked / runtime_review_hit_keyword / runtime_review_preset_response:
      由 ReviewMiddleware.before_model 写入，BaseAgent 兜底处理
    """

    runtime_account_id: NotRequired[Annotated[str, PrivateStateAttr]]
    runtime_app_id: NotRequired[Annotated[str, PrivateStateAttr]]
    runtime_conversation_id: NotRequired[Annotated[str, PrivateStateAttr]]
    runtime_end_user_id: NotRequired[Annotated[str | None, PrivateStateAttr]]
    runtime_invoked_at: NotRequired[Annotated[str, PrivateStateAttr]]
    runtime_workflow_variables: NotRequired[
        Annotated[dict[str, Any], PrivateStateAttr]
    ]
    runtime_review_blocked: NotRequired[Annotated[bool, PrivateStateAttr]]
    runtime_review_hit_keyword: NotRequired[Annotated[str, PrivateStateAttr]]
    runtime_review_preset_response: NotRequired[
        Annotated[str, PrivateStateAttr]
    ]


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
    permissions: Sequence[FilesystemPermission] | None = None,
    skill_sources: Sequence[str] = (),
    memory_sources: Sequence[str] = (),
    subagent_skill_sources: dict[str, Sequence[str]] | None = None,
    end_user_id: str | None = None,
    workflow_variables: dict[str, Any] | None = None,
    subagents: list[dict[str, Any]] | dict[str, dict] | None = None,
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
        subagents: 平台同步 SubAgent 配置。委派使用 DeepAgents 原生
            ``SubAgentMiddleware``；当前阶段不接受异步远端配置。

    Returns:
        CompiledStateGraph，可直接 `agent.invoke(input, config)` 或 `agent.stream(...)`
    """
    normalized_subagents = validate_platform_subagents(subagents)
    middleware = [
        # 事件翻译是 DeepAgents/LangChain 与 MyDify SSE 的唯一业务边界。
        EventTranslationMiddleware(),
        ProjectContextMiddleware(
            account_id=account_id,
            app_id=app_id,
            conversation_id=conversation_id,
            end_user_id=end_user_id,
            workflow_variables=workflow_variables,
        ),
        ReviewMiddleware(review_config=review_config or {}),
        ModelCompatMiddleware(expected_chat_model=llm),
    ]
    if use_checkpointer is not None:
        if checkpointer is not None:
            raise ValueError(
                "checkpointer and use_checkpointer cannot be provided together"
            )
        checkpointer = get_postgres_saver() if use_checkpointer else False

    # The native profile switch prevents DeepAgents from silently adding its
    # broad, parent-tool-inheriting ``general-purpose`` worker.
    disable_implicit_general_purpose(llm)

    # Persisted records are only a business adapter. DeepAgents owns dispatch,
    # stateless context isolation, filesystem middleware and result plumbing.
    subagent_specs = build_subagent_specs(
        normalized_subagents,
        available_tools=tools,
        skill_sources_by_agent=subagent_skill_sources,
    )

    return create_deep_agent(
        model=llm,
        tools=list(tools),
        system_prompt=system_prompt,
        middleware=middleware,
        subagents=subagent_specs,
        skills=list(skill_sources) or None,
        memory=list(memory_sources) or None,
        permissions=list(permissions or []),
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
