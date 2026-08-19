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
                "name VARCHAR(255) NOT NULL DEFAULT '', "
                "invoke_from VARCHAR(255) NOT NULL DEFAULT '', "
                "created_by UUID, "
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


def _add_conversation(
    service,
    app_id,
    conversation_id,
    *,
    created_by=None,
    name="Conversation",
    invoke_from="web_app",
):
    service.db.session.execute(
        text(
            "INSERT INTO conversation "
            "(id, app_id, name, invoke_from, created_by, is_deleted) "
            "VALUES (:id, :app_id, :name, :invoke_from, :created_by, 0)"
        ),
        {
            "id": conversation_id.hex,
            "app_id": app_id.hex,
            "name": name,
            "invoke_from": invoke_from,
            "created_by": (
                created_by.hex if created_by is not None else None
            ),
        },
    )
    service.db.session.commit()


def test_workspace_writes_versions_and_reads_latest(service):
    account_id, app_id, creator_id, conversation_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    _add_conversation(service, app_id, conversation_id)

    first = service.write_text(
        account_id=account_id,
        app_id=app_id,
        conversation_id=conversation_id,
        path="/workspace/guide.md",
        content="v1",
        created_by=creator_id,
    )
    second = service.write_text(
        account_id=account_id,
        app_id=app_id,
        conversation_id=conversation_id,
        path="/workspace/guide.md",
        content="v2",
        created_by=creator_id,
    )

    assert (first.version, second.version) == (1, 2)
    assert service.read_owned(
        account_id=account_id,
        app_id=app_id,
        path="/workspace/guide.md",
        conversation_id=conversation_id,
    ).content == b"v2"
    assert [
        row.version
        for row in service.list_creator_files(
            account_id=account_id,
            app_id=app_id,
            conversation_id=conversation_id,
        )
    ] == [2]


def test_deleted_latest_version_does_not_resurface_old_content(service):
    account_id, app_id, creator_id, conversation_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    _add_conversation(service, app_id, conversation_id)
    service.write_text(
        account_id=account_id,
        app_id=app_id,
        conversation_id=conversation_id,
        path="/workspace/deleted.md",
        content="v1",
        created_by=creator_id,
    )
    service.write_text(
        account_id=account_id,
        app_id=app_id,
        conversation_id=conversation_id,
        path="/workspace/deleted.md",
        content="v2",
        created_by=creator_id,
    )
    service.soft_delete(
        account_id=account_id,
        app_id=app_id,
        path="/workspace/deleted.md",
        conversation_id=conversation_id,
    )

    assert service.list_creator_files(
        account_id=account_id,
        app_id=app_id,
        conversation_id=conversation_id,
    ) == []
    with pytest.raises(FailException):
        service.read_owned(
            account_id=account_id,
            app_id=app_id,
            path="/workspace/deleted.md",
            conversation_id=conversation_id,
        )

    restored = service.write_text(
        account_id=account_id,
        app_id=app_id,
        conversation_id=conversation_id,
        path="/workspace/deleted.md",
        content="v3",
        created_by=creator_id,
    )
    assert restored.version == 3


def test_end_user_read_rejects_other_conversation(service):
    account_id, app_id, creator_id = uuid4(), uuid4(), uuid4()
    own_conversation, other_conversation = uuid4(), uuid4()
    _add_conversation(service, app_id, own_conversation)
    _add_conversation(service, app_id, other_conversation)
    service.write_text(
        account_id=account_id,
        app_id=app_id,
        conversation_id=own_conversation,
        path="/workspace/own.md",
        content="own",
        created_by=creator_id,
    )
    service.write_text(
        account_id=account_id,
        app_id=app_id,
        conversation_id=other_conversation,
        path="/workspace/private.md",
        content="other",
        created_by=creator_id,
    )

    with pytest.raises(FailException):
        service.read_owned(
            account_id=account_id,
            app_id=app_id,
            path="/workspace/private.md",
            conversation_id=own_conversation,
        )


def test_end_user_lists_all_owned_web_app_files_and_searches_virtual_paths(
    service,
):
    account_id, app_id, end_user_id, other_owner = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    first_conversation, second_conversation, private_conversation = (
        uuid4(),
        uuid4(),
        uuid4(),
    )
    _add_conversation(
        service,
        app_id,
        first_conversation,
        created_by=end_user_id,
        name="产品资料",
    )
    _add_conversation(
        service,
        app_id,
        second_conversation,
        created_by=end_user_id,
        name="账单记录",
    )
    _add_conversation(
        service,
        app_id,
        private_conversation,
        created_by=other_owner,
        name="不属于当前用户",
    )

    first_file = service.write_text(
        account_id=account_id,
        app_id=app_id,
        conversation_id=first_conversation,
        path="/workspace/notes/overview.md",
        content="first",
        created_by=end_user_id,
    )
    second_file = service.write_text(
        account_id=account_id,
        app_id=app_id,
        conversation_id=second_conversation,
        path="/workspace/billing.csv",
        content="second",
        created_by=end_user_id,
    )
    service.write_text(
        account_id=account_id,
        app_id=app_id,
        conversation_id=private_conversation,
        path="/workspace/private.txt",
        content="private",
        created_by=other_owner,
    )

    all_files = service.list_end_user_all_files(
        account_id=account_id,
        app_id=app_id,
        owner_ids=[end_user_id],
    )
    assert {row.id for row in all_files} == {first_file.id, second_file.id}

    path_matches = service.list_end_user_all_files(
        account_id=account_id,
        app_id=app_id,
        owner_ids=[end_user_id],
        query="/workspace/notes/overview.md",
    )
    assert [row.id for row in path_matches] == [first_file.id]

    name_matches = service.list_end_user_all_files(
        account_id=account_id,
        app_id=app_id,
        owner_ids=[end_user_id],
        query="账单",
    )
    assert [row.id for row in name_matches] == [second_file.id]


def test_workspace_rejects_conversation_from_another_app(service):
    account_id, app_id, other_app_id, creator_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    conversation_id = uuid4()
    _add_conversation(service, other_app_id, conversation_id)

    with pytest.raises(FailException):
        service.write_text(
            account_id=account_id,
            app_id=app_id,
            conversation_id=conversation_id,
            path="/workspace/private.md",
            content="x",
            created_by=creator_id,
        )


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/seed/escape",
        "/artifacts/escape",
        "/workspace/../escape",
        "/workspace/\x00bad",
        "/workspace/\ncontrol",
        "/workspace/.audit/private.json",
        "/workspace/.system/private.json",
        "/workspace/" + ("a" * 600),
    ],
)
def test_workspace_rejects_unsafe_paths(service, path):
    account_id, app_id, conversation_id = uuid4(), uuid4(), uuid4()
    _add_conversation(service, app_id, conversation_id)
    with pytest.raises(FailException):
        service.write_text(
            account_id=account_id,
            app_id=app_id,
            conversation_id=conversation_id,
            path=path,
            content="x",
            created_by=uuid4(),
        )


def test_workspace_service_enforces_configured_quota(service, monkeypatch):
    """Creator writes use the same byte and file quota as Agent writes."""
    monkeypatch.setenv("WORKSPACE_MAX_BYTES", "5")
    monkeypatch.setenv("WORKSPACE_MAX_FILES", "1")
    account_id, app_id, creator_id, conversation_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    _add_conversation(service, app_id, conversation_id)

    service.write_text(
        account_id=account_id,
        app_id=app_id,
        conversation_id=conversation_id,
        path="/workspace/one.txt",
        content="12345",
        created_by=creator_id,
    )

    with pytest.raises(FailException) as exc_info:
        service.write_text(
            account_id=account_id,
            app_id=app_id,
            conversation_id=conversation_id,
            path="/workspace/two.txt",
            content="x",
            created_by=creator_id,
        )
    assert "file quota exceeded" in exc_info.value.message
