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
            if not path.startswith(("/workspace/", "/artifacts/"))
        }
        return CompositeBackend(
            default=sandbox,
            routes=sandbox_routes,
            artifacts_root="/",
        )
    return CompositeBackend(
        default=StateBackend(),
        routes=dict(routes),
        artifacts_root="/",
    )


__all__ = ["build_composite_backend"]
