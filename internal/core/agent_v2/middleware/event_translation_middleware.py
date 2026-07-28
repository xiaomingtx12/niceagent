"""R0 deepagents 事件流 → my-dify 既有 AgentThought/QueueEvent 翻译中间件。

master 的 SSE 端点 (chat / debug_chat) 已经按 `AgentThought` + `QueueEvent` 协议
往下游推消息。引入 deepagents 之后，图的 streaming 产出的是 langgraph 原生
事件流：`{node: ..., events: [...]}`，本中间件负责在 `after_agent` 之前把
deepagents 内部的 model/tool/todo 事件翻译成 `AgentThought`，写到 state
的 `agent_thoughts` 字段，stream 时由 BaseAgent / app_handler 读这个字段。

设计要点：
- 不在 middleware 里直接发 SSE（middleware 跑在同步图节点上，发 SSE 会阻塞
  graph 调度）；只把翻译结果存 state，stream 端点遍历 state 一次性发出去
- 翻译逻辑挂在 `wrap_model_call`（拿到 AIMessage 的 tool_calls/usage）和
  `wrap_tool_call`（拿到 ToolMessage 的 output）两个钩子
- 用 Pydantic `AgentThought` 序列化，与 master schema 完全一致
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional
from uuid import UUID, uuid4

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from internal.core.agent.entities.queue_entity import AgentThought, QueueEvent


def _new_thought_id() -> UUID:
    return uuid4()


class EventTranslationMiddleware(AgentMiddleware):
    """deepagents 内部事件 → AgentThought 翻译。

    state schema 字段：
    - `task_id: UUID` —— 每次 invoke 由 before_agent 注入
    - `agent_thoughts: list[AgentThought]` —— 累计追加，供 stream 端点读

    中间件本身不感知上层业务（不感知 app_id/conversation_id），这些上下文走
    `runtime.context`（deepagents 1.x 引入的 Runtime context schema）由上层
    factory 注入。
    """

    def __init__(self, *, task_id: Optional[UUID] = None) -> None:
        super().__init__()
        self._task_id = task_id or uuid4()
        self._step_started_at = 0.0

    # --- hooks -----------------------------------------------------------

    def before_agent(self, state: dict, runtime: Any) -> dict | None:
        """注入 task_id 到 state，初始化 thoughts 列表。"""
        state.setdefault("task_id", self._task_id)
        state.setdefault("agent_thoughts", [])
        return None

    def wrap_model_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        """在 LLM 调用前后加 thought：AGENT_THOUGHT（带 tool_calls/usage）。"""
        self._step_started_at = time.perf_counter()
        response = handler(request)
        # response 是 ModelResponse[AIMessage]
        ai_message: AIMessage = getattr(response, "result", None) or response
        state = request.state if hasattr(request, "state") else {}
        task_id = state.get("task_id", self._task_id)

        tool_calls = ai_message.tool_calls or []
        # 单一文本输出 → 走 AGENT_THOUGHT；带 tool_calls → 走 AGENT_MESSAGE
        if tool_calls:
            for tc in tool_calls:
                tool_name = tc.get("name", "")
                tool_input = tc.get("args", {}) or {}
                is_subagent_delegate = tool_name == "task"
                thought = AgentThought(
                    id=_new_thought_id(),
                    task_id=task_id,
                    event=(
                        QueueEvent.SUB_AGENT_DELEGATE
                        if is_subagent_delegate
                        else QueueEvent.AGENT_ACTION
                    ),
                    thought=ai_message.content if isinstance(ai_message.content, str) else "",
                    tool=(
                        tool_input.get("subagent_type", "task")
                        if is_subagent_delegate
                        else tool_name
                    ),
                    tool_input=tool_input,
                    message_token_count=getattr(ai_message.usage_metadata, "input_tokens", 0) or 0,
                    answer_token_count=getattr(ai_message.usage_metadata, "output_tokens", 0) or 0,
                    total_token_count=getattr(ai_message.usage_metadata, "total_tokens", 0) or 0,
                    latency=time.perf_counter() - self._step_started_at,
                )
                _append_thought(state, thought)
        else:
            thought = AgentThought(
                id=_new_thought_id(),
                task_id=task_id,
                event=QueueEvent.AGENT_THOUGHT,
                thought=ai_message.content if isinstance(ai_message.content, str) else "",
                message_token_count=getattr(ai_message.usage_metadata, "input_tokens", 0) or 0,
                answer_token_count=getattr(ai_message.usage_metadata, "output_tokens", 0) or 0,
                total_token_count=getattr(ai_message.usage_metadata, "total_tokens", 0) or 0,
                latency=time.perf_counter() - self._step_started_at,
            )
            _append_thought(state, thought)
        return response

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        """工具调用后追加 AGENT_MESSAGE + AGENT_ACTION（含 observation + tool 名称）。"""
        result = handler(request)
        tool_call = request.tool_call
        tool_msg = _extract_tool_message(result)
        if tool_msg is None:
            return result

        state = request.state if hasattr(request, "state") else {}
        task_id = state.get("task_id", self._task_id)
        observation = (
            tool_msg.content
            if isinstance(tool_msg.content, str)
            else str(tool_msg.content)
        )
        tool_name = tool_call.get("name", "")
        tool_input = tool_call.get("args", {}) or {}
        is_subagent_result = tool_name == "task"
        thought = AgentThought(
            id=_new_thought_id(),
            task_id=task_id,
            event=(
                QueueEvent.SUB_AGENT_END
                if is_subagent_result
                else QueueEvent.AGENT_ACTION
            ),
            tool=(
                tool_input.get("subagent_type", "task")
                if is_subagent_result
                else tool_name
            ),
            tool_input=tool_input,
            observation=observation,
            latency=time.perf_counter() - self._step_started_at,
        )
        _append_thought(state, thought)
        return result

    def after_agent(self, state: dict, runtime: Any) -> dict | None:
        """结束前追加 AGENT_END thought。"""
        task_id = state.get("task_id", self._task_id)
        end_thought = AgentThought(
            id=_new_thought_id(),
            task_id=task_id,
            event=QueueEvent.AGENT_END,
            latency=time.perf_counter() - self._step_started_at,
        )
        _append_thought(state, end_thought)
        return None


def _append_thought(state: dict, thought: AgentThought) -> None:
    """往 state['agent_thoughts'] 追加；state 是 mutable dict，跟 deepagents 一致。"""
    thoughts = state.setdefault("agent_thoughts", [])
    thoughts.append(thought)


def _extract_tool_message(result: Any) -> Optional[ToolMessage]:
    """从 LangChain 工具返回值提取 ToolMessage。

    DeepAgents 的同步 `task` 工具返回 `Command(update={"messages": [...]})`，
    普通工具通常直接返回 `ToolMessage`。两种形式都归一化，才能在 sub-agent
    完成时发出明确的边界事件。
    """
    if isinstance(result, ToolMessage):
        return result
    if not isinstance(result, Command) or not isinstance(result.update, dict):
        return None
    messages = result.update.get("messages", [])
    if not isinstance(messages, list):
        return None
    return next(
        (message for message in reversed(messages) if isinstance(message, ToolMessage)),
        None,
    )


__all__ = ["EventTranslationMiddleware"]
