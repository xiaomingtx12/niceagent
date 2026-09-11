"""装配阶段交给执行器的运行资源及其所有权。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore

from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.backends.composition.composite_backend_factory import close_runtime_backends


@dataclass(slots=True)
class AgentRuntimeResources:
    """持有本轮独占 Backend；共享 Store 和 Checkpointer 由进程管理。"""

    checkpointer: BaseCheckpointSaver | bool
    backend: BackendProtocol
    permissions: list[FilesystemPermission]
    skill_sources: list[str]
    memory_sources: list[str]
    subagent_skill_sources: Mapping[str, list[str]]
    runtime_context: AgentRunContext
    store: BaseStore | None = None
    _close_attempted: bool = field(default=False, init=False, repr=False, compare=False)
    _close_error: BaseException | None = field(default=None, init=False, repr=False, compare=False)

    def close(self) -> None:
        """清理只执行一次；失败持续可见，避免重跑已释放会话的沙箱同步。"""
        if self._close_error is not None:
            raise self._close_error
        if self._close_attempted:
            return
        self._close_attempted = True
        try:
            close_runtime_backends(self.backend)
        except BaseException as exc:
            self._close_error = exc
            raise


__all__ = ["AgentRuntimeResources"]
