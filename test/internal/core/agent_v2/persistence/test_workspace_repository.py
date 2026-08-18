"""Workspace repository concurrency tests."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from internal.core.agent_v2.persistence.workspace import (
    WorkspaceRepository,
)
from internal.model import WorkspaceFile


def test_concurrent_writes_allocate_distinct_versions(tmp_path):
    """Concurrent repositories serialize version allocation per app."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'workspace.db'}",
        connect_args={"check_same_thread": False},
    )
    WorkspaceFile.__table__.create(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    account_id, app_id, conversation_id = uuid4(), uuid4(), uuid4()

    def write(content: str) -> int:
        session = sessions()
        try:
            repository = WorkspaceRepository(
                session=session,
                account_id=account_id,
                app_id=app_id,
                conversation_id=conversation_id,
            )
            return repository.write_text(
                f"{conversation_id}/result.txt", content
            ).version
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        versions = list(executor.map(write, ["first", "second"]))

    assert sorted(versions) == [1, 2]
    engine.dispose()
