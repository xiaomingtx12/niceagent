"""装配阶段交给执行器的运行资源及其所有权。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from contextlib import ExitStack

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore

from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.backends.composition.composite_backend_factory import close_runtime_backends


@dataclass(frozen=True, slots=True)
class AgentRuntimeResources:
    """承接装配器交出的独占 Backend 与关闭回调，执行器只通过此对象释放资源。"""

    checkpointer: BaseCheckpointSaver | bool
    backend: BackendProtocol
    permissions: list[FilesystemPermission]
    skill_sources: list[str]
    memory_sources: list[str]
    subagent_skill_sources: Mapping[str, list[str]]
    runtime_context: AgentRunContext
    store: BaseStore | None = None
    _cleanup: ExitStack | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        """独立构造时默认持有 Backend；装配器传入的回调已包含完整所有权。"""
        if self._cleanup is None:
            cleanup = ExitStack()
            cleanup.callback(close_runtime_backends, self.backend)
            object.__setattr__(self, "_cleanup", cleanup)

    def close(self) -> None:
        """消费关闭回调一次；共享的 Store 和 Checkpointer 由进程管理，不随 Run 关闭。"""
        if self._cleanup is not None:
            self._cleanup.close()


__all__ = ["AgentRuntimeResources"]
