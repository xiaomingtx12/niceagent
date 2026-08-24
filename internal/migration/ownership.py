"""Database objects whose schema is managed outside Alembic."""

from collections.abc import Callable
from typing import Any


LANGGRAPH_MANAGED_TABLES = frozenset(
    {
        "checkpoint_blobs",
        "checkpoint_migrations",
        "checkpoint_writes",
        "checkpoints",
        "store",
        "store_migrations",
    }
)

IncludeName = Callable[[str | None, str, dict[str, Any]], bool]


def langgraph_aware_include_name(
    existing: IncludeName | None = None,
) -> IncludeName:
    """Compose an Alembic name filter that hides LangGraph-owned tables."""

    def include_name(
        name: str | None,
        type_: str,
        parent_names: dict[str, Any],
    ) -> bool:
        if type_ == "table" and name in LANGGRAPH_MANAGED_TABLES:
            return False
        if existing is None:
            return True
        return existing(name, type_, parent_names)

    return include_name
