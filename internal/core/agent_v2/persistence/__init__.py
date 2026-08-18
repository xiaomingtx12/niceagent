"""Agent V2 持久化适配器的延迟导出。"""

from importlib import import_module

_EXPORTS = {
    "BaseCheckpointSaver": (
        "internal.core.agent_v2.persistence.checkpoint",
        "BaseCheckpointSaver",
    ),
    "CheckpointProvider": (
        "internal.core.agent_v2.persistence.checkpoint",
        "CheckpointProvider",
    ),
    "PostgresSaver": (
        "internal.core.agent_v2.persistence.checkpoint",
        "PostgresSaver",
    ),
    "build_thread_id": (
        "internal.core.agent_v2.persistence.checkpoint",
        "build_thread_id",
    ),
    "close_connection_pool": (
        "internal.core.agent_v2.persistence.checkpoint",
        "close_connection_pool",
    ),
    "close_postgres_saver": (
        "internal.core.agent_v2.persistence.checkpoint",
        "close_postgres_saver",
    ),
    "create_postgres_saver": (
        "internal.core.agent_v2.persistence.checkpoint",
        "create_postgres_saver",
    ),
    "get_configured_checkpointer": (
        "internal.core.agent_v2.persistence.checkpoint",
        "get_configured_checkpointer",
    ),
    "get_postgres_saver": (
        "internal.core.agent_v2.persistence.checkpoint",
        "get_postgres_saver",
    ),
    "WorkspaceRepository": (
        "internal.core.agent_v2.persistence.workspace",
        "WorkspaceRepository",
    ),
    "WorkspaceVersionConflict": (
        "internal.core.agent_v2.persistence.workspace",
        "WorkspaceVersionConflict",
    ),
    "StoreProvider": (
        "internal.core.agent_v2.persistence.store",
        "StoreProvider",
    ),
    "close_postgres_store": (
        "internal.core.agent_v2.persistence.store",
        "close_postgres_store",
    ),
    "create_postgres_store": (
        "internal.core.agent_v2.persistence.store",
        "create_postgres_store",
    ),
    "get_configured_store": (
        "internal.core.agent_v2.persistence.store",
        "get_configured_store",
    ),
    "get_postgres_store": (
        "internal.core.agent_v2.persistence.store",
        "get_postgres_store",
    ),
}


def __getattr__(name: str):
    """按需加载持久化对象，避免 Backend 和仓储互相初始化。"""
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    return getattr(import_module(module_name), attribute)


__all__ = list(_EXPORTS)
