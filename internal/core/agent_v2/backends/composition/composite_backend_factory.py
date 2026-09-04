"""Agent V2 运行时 CompositeBackend 的统一装配入口。"""

from __future__ import annotations

from collections.abc import Mapping

from deepagents.backends import CompositeBackend, StateBackend
from deepagents.backends.protocol import (
    BackendProtocol,
    SandboxBackendProtocol,
)


def build_composite_backend(
    *,
    routes: Mapping[str, BackendProtocol],
    sandbox: SandboxBackendProtocol | None = None,
) -> CompositeBackend:
    """构造当前运行的文件数据平面。

    非 Sandbox 模式使用 DeepAgents 原生 ``StateBackend`` 承载线程内
    临时文件和框架内部文件；业务数据通过显式虚拟路径路由接入。
    Sandbox 模式把官方 Sandbox 放到 default，保证文件工具和
    ``execute`` 使用同一个隔离文件系统。
    """
    if sandbox is not None:
        sandbox_routes = {
            path: route
            for path, route in routes.items()
            if not path.startswith("/workspace/")
        }
        # DeepAgents 目前要求启用 execute 的 CompositeBackend 将每条
        # FilesystemPermission 规则映射到某个路由。根路由继续指向同一个
        # Sandbox，专用的 Skill/Memory 路由仍会因更长前缀优先而生效。
        sandbox_routes["/"] = sandbox
        return CompositeBackend(
            default=sandbox,
            routes=sandbox_routes,
            artifacts_root="/workspace",
        )
    return CompositeBackend(
        default=StateBackend(),
        routes=dict(routes),
        artifacts_root="/workspace",
    )


def close_runtime_backends(backend: BackendProtocol | None) -> None:
    """关闭 CompositeBackend 及其路由 Backend，并避免共享实例重复关闭。"""
    if backend is None:
        return
    candidates = [backend, getattr(backend, "default", None)]
    routes = getattr(backend, "routes", {}) or {}
    candidates.extend(routes.values())
    seen: set[int] = set()
    errors: list[Exception] = []
    for candidate in candidates:
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        close = getattr(candidate, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
    if errors:
        raise RuntimeError(
            f"failed to close {len(errors)} runtime backend(s)"
        ) from errors[0]
