"""装配阶段交给执行器的运行资源及其所有权。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore

from internal.core.agent_v2.observability.telemetry import AgentTelemetry
from internal.core.agent_v2.entity.run_context import AgentRunContext


@dataclass(frozen=True, slots=True)
class AgentRuntimeResources:
    """装配层交给 Agent Core 的一次运行时资源快照。"""

    checkpointer: BaseCheckpointSaver | bool
    backend: BackendProtocol
    permissions: list[FilesystemPermission]
    skill_sources: list[str]
    memory_sources: list[str]
    subagent_skill_sources: Mapping[str, list[str]]
    telemetry: AgentTelemetry
    runtime_context: AgentRunContext
    store: BaseStore | None = None


__all__ = ["AgentRuntimeResources"]
