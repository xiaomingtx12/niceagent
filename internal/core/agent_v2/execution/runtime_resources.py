"""装配阶段交给执行器的运行资源及其所有权。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from deepagents.backends import CompositeBackend
from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore

from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.backends.workspace.workspace_projection import WorkspaceProjection
from internal.core.agent_v2.persistence.workspace_store import WorkspaceStore
from internal.core.agent_v2.execution.workspace_delivery import WorkspaceDelivery

if TYPE_CHECKING:
    from internal.core.agent_v2.assembly.context.skill_context import SkillProjection


@dataclass(slots=True)
class AgentRuntimeResources:
    """持有本轮独占 Backend；共享 Store 和 Checkpointer 由进程管理。"""

    checkpointer: BaseCheckpointSaver | bool
    backend: BackendProtocol
    workspace: WorkspaceProjection
    workspace_store: WorkspaceStore
    file_delivery: WorkspaceDelivery
    skill_projection: SkillProjection
    permissions: list[FilesystemPermission]
    memory_sources: list[str]
    runtime_context: AgentRunContext
    store: BaseStore | None = None
    _close_attempted: bool = field(default=False, init=False, repr=False, compare=False)
    _close_error: BaseException | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def execution_uses_virtual_paths(self) -> bool:
        """集中读取默认执行器的路径语义，图与中间件不分别遍历组合结构。"""
        backend = getattr(self.backend, "default", self.backend)
        return bool(getattr(backend, "execution_uses_virtual_paths", False))

    def close(self) -> None:
        """只释放资源，不提交文件；关闭失败持续可见，避免重复操作已释放资源。"""
        if self._close_error is not None:
            raise self._close_error
        if self._close_attempted:
            return
        self._close_attempted = True
        errors: list[BaseException] = []
        try:
            # 1.先释放或归还 Sandbox，整个清理路径不执行 COS 上传或文件扫描。
            close_runtime_backends(self.backend)
        except BaseException as exc:
            errors.append(exc)
        # 2.逐项释放存储访问会话和临时目录，个别失败不能阻断其余清理。
        for resource in (self.workspace_store, self.workspace, self.skill_projection):
            try:
                resource.close()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            self._close_error = BaseExceptionGroup("failed to close runtime resources", errors)
            raise self._close_error


def close_runtime_backends(*backends: BackendProtocol | None) -> None:
    """装配失败和运行结束共用释放流程；按实例去重，个别失败仍继续清理。"""
    # 1.只展开框架 Composite 的直接路由，不反射包装器内部借用的资源。
    candidates = []
    for backend in backends:
        candidates.append(backend)
        if isinstance(backend, CompositeBackend):
            candidates.append(backend.default)
            candidates.extend(backend.routes.values())
    seen: set[int] = set()
    errors: list[BaseException] = []
    # 2.默认执行器与根路由可能是同一实例，只释放一次。
    for candidate in candidates:
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        close = getattr(candidate, "close", None)
        if callable(close):
            try:
                close()
            except BaseException as exc:
                errors.append(exc)
    # 3.全部尝试后汇总错误，保证单项失败不会遗留其他资源。
    if errors:
        raise BaseExceptionGroup("failed to close runtime backends", errors)


__all__ = ["AgentRuntimeResources", "close_runtime_backends"]
