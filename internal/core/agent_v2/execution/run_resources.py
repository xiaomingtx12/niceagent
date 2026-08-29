"""一次 Agent Run 持有的 Deep Agents 框架资源。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore

from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.assembly.observability.telemetry import AgentTelemetry


@dataclass(frozen=True, slots=True)
class AgentRuntimeResources:
    """承载一次 Run 的 Backend、Store、Checkpointer 和权限。"""

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
