"""Workspace versioning and visibility tests."""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from internal.exception import FailException
from internal.model import WorkspaceFile
from internal.service.workspace_file_service import WorkspaceFileService


@pytest.fixture
def service():
    import uuid as _uuid

    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _register(dbapi_conn, _record):
        dbapi_conn.create_function(
            "uuid_generate_v4",
            0,
            lambda: str(_uuid.uuid4()),
        )

    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE conversation ("
                "id UUID PRIMARY KEY, "
                "app_id UUID NOT NULL, "
                "is_deleted BOOLEAN NOT NULL DEFAULT 0"
                ")"
            )
        )
    WorkspaceFile.__table__.create(engine, checkfirst=True)
    session = sessionmaker(bind=engine)()
    instance = WorkspaceFileService.__new__(WorkspaceFileService)
    instance.db = type("FakeDB", (), {"session": session})()
    yield instance
    session.close()
    engine.dispose()


def test_workspace_writes_versions_and_reads_latest(service):
    account_id, app_id, creator_id = uuid4(), uuid4(), uuid4()
    first = service.write_text(
        account_id=account_id,
        app_id=app_id,
        conversation_id=None,
        path="seed/guide.md",
        content="v1",
        created_by=creator_id,
    )
    second = service.write_text(
        account_id=account_id,
        app_id=app_id,
        conversation_id=None,
        path="/seed/guide.md",
        content="v2",
        created_by=creator_id,
    )

    assert (first.version, second.version) == (1, 2)
    assert service.read_owned(
        account_id=account_id,
        app_id=app_id,
        path="seed/guide.md",
    ).content == b"v2"
    assert [row.version for row in service.list_creator_files(
        account_id=account_id,
        app_id=app_id,
    )] == [2]


def test_deleted_latest_version_does_not_resurface_old_content(service):
    account_id, app_id, creator_id = uuid4(), uuid4(), uuid4()
    service.write_text(
        account_id=account_id,
        app_id=app_id,
        conversation_id=None,
        path="seed/deleted.md",
        content="v1",
        created_by=creator_id,
    )
    service.write_text(
        account_id=account_id,
        app_id=app_id,
        conversation_id=None,
        path="seed/deleted.md",
        content="v2",
        created_by=creator_id,
    )
    service.soft_delete(
        account_id=account_id,
        app_id=app_id,
        path="seed/deleted.md",
    )

    assert service.list_creator_files(
        account_id=account_id,
        app_id=app_id,
    ) == []
    with pytest.raises(FailException):
        service.read_owned(
            account_id=account_id,
            app_id=app_id,
            path="seed/deleted.md",
        )

    restored = service.write_text(
        account_id=account_id,
        app_id=app_id,
        conversation_id=None,
        path="seed/deleted.md",
        content="v3",
        created_by=creator_id,
    )
    assert restored.version == 3


def test_end_user_only_reads_current_conversation(service):
    account_id, app_id, creator_id = uuid4(), uuid4(), uuid4()
    own_conversation, other_conversation = uuid4(), uuid4()
    service.db.session.execute(
        text(
            "INSERT INTO conversation (id, app_id, is_deleted) "
            "VALUES (:own_id, :app_id, 0), (:other_id, :app_id, 0)"
        ),
        {
            "own_id": own_conversation.hex,
            "other_id": other_conversation.hex,
            "app_id": app_id.hex,
        },
    )
    service.db.session.commit()
    own = service.write_text(
        account_id=account_id,
        app_id=app_id,
        conversation_id=own_conversation,
        path=f"{own_conversation}/conversation_history/{own_conversation}.md",
        content="own history",
        created_by=creator_id,
    )
    service.write_text(
        account_id=account_id,
        app_id=app_id,
        conversation_id=other_conversation,
        path=f"{other_conversation}/private.md",
        content="other",
        created_by=creator_id,
    )

    assert service.list_end_user_files(
        account_id=account_id,
        app_id=app_id,
        conversation_id=own_conversation,
    ) == [own]
    with pytest.raises(FailException):
        service.read_owned(
            account_id=account_id,
            app_id=app_id,
            path=f"{other_conversation}/private.md",
            end_user_conversation_id=own_conversation,
        )


def test_workspace_rejects_conversation_from_another_app(service):
    account_id, app_id, other_app_id, creator_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    conversation_id = uuid4()
    service.db.session.execute(
        text(
            "INSERT INTO conversation (id, app_id, is_deleted) "
            "VALUES (:id, :app_id, 0)"
        ),
        {"id": conversation_id.hex, "app_id": other_app_id.hex},
    )
    service.db.session.commit()

    with pytest.raises(FailException):
        service.write_text(
            account_id=account_id,
            app_id=app_id,
            conversation_id=conversation_id,
            path=f"{conversation_id}/private.md",
            content="x",
            created_by=creator_id,
        )


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "seed/../escape",
        "seed/\x00bad",
        "seed/\ncontrol",
        "seed/" + ("a" * 600),
    ],
)
def test_workspace_rejects_unsafe_paths(service, path):
    with pytest.raises(FailException):
        service.write_text(
            account_id=uuid4(),
            app_id=uuid4(),
            conversation_id=None,
            path=path,
            content="x",
            created_by=uuid4(),
        )
