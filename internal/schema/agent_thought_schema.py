"""Shared serializer for persisted Agent V2 trace rows."""

from __future__ import annotations

from typing import Any

from internal.lib.helper import datetime_to_timestamp
from internal.model import MessageAgentThought


def dump_agent_thought(agent_thought: MessageAgentThought) -> dict[str, Any]:
    """Serialize one persisted trace row for conversation APIs."""
    return {
        "id": agent_thought.id,
        "position": agent_thought.position,
        "event": agent_thought.event,
        "thought": agent_thought.thought,
        "observation": agent_thought.observation,
        "tool": agent_thought.tool,
        "tool_input": agent_thought.tool_input,
        "agent_id": agent_thought.agent_id,
        "agent_type": agent_thought.agent_type,
        "parent_agent_id": agent_thought.parent_agent_id,
        "delegation_id": agent_thought.delegation_id,
        "task_id": agent_thought.task_id,
        "todos": agent_thought.todos,
        "latency": agent_thought.latency,
        "created_at": datetime_to_timestamp(agent_thought.created_at),
    }


__all__ = ["dump_agent_thought"]

