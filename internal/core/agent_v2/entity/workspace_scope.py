"""Agent Workspace 的稳定归属作用域。"""

from __future__ import annotations

from uuid import NAMESPACE_URL, UUID, uuid5


_WORKSPACE_SCOPE_PREFIX = "my-dify:agent-workspace:v1"


def build_workspace_scope_id(
    account_id: UUID,
    app_id: UUID,
) -> UUID:
    """为账号与应用生成跨会话稳定的 Workspace 作用域 ID。

    v1 名称中的末尾账号段属于已持久化的兼容契约；修改名称格式前必须迁移
    既有 Workspace 数据。
    """
    return uuid5(
        NAMESPACE_URL,
        f"{_WORKSPACE_SCOPE_PREFIX}:{account_id}:{app_id}:{account_id}",
    )


__all__ = ["build_workspace_scope_id"]
