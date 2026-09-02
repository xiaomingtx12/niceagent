"""Agent Workspace 的稳定归属作用域。"""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5


_WORKSPACE_SCOPE_PREFIX = "my-dify:agent-workspace:v1"


def build_workspace_scope_id(
    account_id: UUID,
    app_id: UUID,
) -> UUID:
    """为账号和 Agent 生成稳定的内部 Workspace 作用域 ID。

    ``WorkspaceFile`` 当前使用 ``conversation_id`` 作为既有物理作用域字段。
    这里生成一个稳定 UUID 作为兼容层键值，使不同会话和入口仍能复用同一
    账号级 Workspace。命名格式保留 v1，并把账号作为历史第三段值，兼容
    原先 Creator 入口已经生成的账号级作用域。
    """
    return uuid5(
        NAMESPACE_URL,
        f"{_WORKSPACE_SCOPE_PREFIX}:{account_id}:{app_id}:{account_id}",
    )


__all__ = ["build_workspace_scope_id"]
