"""Agent V2 业务事件的稳定序列化。"""

from __future__ import annotations

from typing import Any

from internal.core.agent_v2.agents.models import AgentThought

PUBLIC_EVENT_FIELDS = {
    "event",
    "thought",
    "observation",
    "tool",
    "tool_input",
    "answer",
    "latency",
    "total_token_count",
    "total_price",
    "agent_id",
    "agent_type",
    "parent_agent_id",
    "delegation_id",
    "background_task_id",
    "todos",
}


def serialize_agent_thought(thought: AgentThought) -> dict[str, Any]:
    """序列化事件且不包含传输层标识。"""
    return thought.model_dump(include=PUBLIC_EVENT_FIELDS, mode="json")


__all__ = ["serialize_agent_thought"]
