from uuid import uuid4

import pytest
from langgraph.store.memory import InMemoryStore

from internal.exception import FailException
from internal.service.memory_file_service import MemoryFileService


@pytest.fixture
def service():
    store = InMemoryStore()
    return MemoryFileService(store_provider=lambda: store)


def test_memory_files_are_persistent_and_user_scoped(service):
    account_id, app_id = uuid4(), uuid4()
    first_user, second_user = uuid4(), uuid4()

    service.write_file(
        account_id=account_id,
        app_id=app_id,
        end_user_id=first_user,
        path="/memories/preferences.md",
        content="likes concise answers",
    )

    assert [
        row.path
        for row in service.list_files(
            account_id=account_id,
            app_id=app_id,
            end_user_id=first_user,
        )
    ] == ["/memories/preferences.md"]
    assert service.list_files(
        account_id=account_id,
        app_id=app_id,
        end_user_id=second_user,
    ) == []


def test_memory_file_update_preserves_creation_and_changes_content(service):
    account_id, app_id, end_user_id = uuid4(), uuid4(), uuid4()
    first = service.write_file(
        account_id=account_id,
        app_id=app_id,
        end_user_id=end_user_id,
        path="/memories/AGENTS.md",
        content="v1",
    )
    second = service.write_file(
        account_id=account_id,
        app_id=app_id,
        end_user_id=end_user_id,
        path="/memories/AGENTS.md",
        content="v2",
    )

    assert second.content == "v2"
    assert second.created_at == first.created_at
    assert second.updated_at != first.updated_at


def test_memory_file_delete_removes_only_owned_file(service):
    account_id, app_id, end_user_id = uuid4(), uuid4(), uuid4()
    service.write_file(
        account_id=account_id,
        app_id=app_id,
        end_user_id=end_user_id,
        path="/memories/delete-me.md",
        content="temporary",
    )

    service.delete_file(
        account_id=account_id,
        app_id=app_id,
        end_user_id=end_user_id,
        path="/memories/delete-me.md",
    )

    with pytest.raises(FailException):
        service.read_file(
            account_id=account_id,
            app_id=app_id,
            end_user_id=end_user_id,
            path="/memories/delete-me.md",
        )


@pytest.mark.parametrize(
    "path",
    [
        "/workspace/not-memory.md",
        "/memories/",
        "/memories/../escape.md",
        "/memories/.system/private.md",
        "/memories/\x00bad.md",
    ],
)
def test_memory_file_rejects_invalid_paths(service, path):
    with pytest.raises(FailException):
        service.write_file(
            account_id=uuid4(),
            app_id=uuid4(),
            end_user_id=uuid4(),
            path=path,
            content="x",
        )
