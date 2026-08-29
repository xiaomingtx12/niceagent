import json
from types import SimpleNamespace
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy import MetaData
from sqlalchemy.orm import sessionmaker

from internal.entity.agent_attachment_entity import AgentAttachment
from internal.exception import (
    AgentAttachmentException,
    ForbiddenException,
    ValidateErrorException,
)
from internal.model import UploadFile
from internal.schema.agent_attachment_schema import AgentAttachmentResolveReq
from internal.service.agent_attachment_service import AgentAttachmentService


@pytest.fixture()
def session():
    """创建只包含上传文件表的 SQLite 测试会话。"""
    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _register_uuid_function(dbapi_conn, _record):
        dbapi_conn.create_function("uuid_generate_v4", 0, lambda: str(uuid4()))

    test_table = UploadFile.__table__.to_metadata(MetaData())
    for column in test_table.columns:
        column.server_default = None
    test_table.create(engine)
    session_factory = sessionmaker(bind=engine)
    db_session = session_factory()
    yield db_session
    db_session.close()
    engine.dispose()


@pytest.fixture()
def service(session):
    return AgentAttachmentService(
        db=SimpleNamespace(session=session),
        cos_service=SimpleNamespace(
            get_file_url=lambda key: f"https://files.example/{key}"
        ),
    )


def _upload(account_id, *, name="report.pdf", key="report-key"):
    now = datetime.now(timezone.utc)
    return UploadFile(
        id=uuid4(),
        account_id=account_id,
        name=name,
        key=key,
        size=12,
        extension=name.rsplit(".", 1)[-1],
        mime_type="application/pdf",
        hash="hash",
        updated_at=now,
        created_at=now,
    )


def test_resolve_preserves_submission_order_and_uses_owned_files(service, session):
    account_id = uuid4()
    first = _upload(account_id, name="first.pdf", key="first-key")
    second = _upload(account_id, name="second.pdf", key="second-key")
    session.add_all([first, second])
    session.commit()

    attachments = service.resolve(
        AgentAttachmentResolveReq.from_values(
            [str(second.id), first.id],
            owner_id=account_id,
        )
    )

    assert [item.id for item in attachments] == [second.id, first.id]
    assert [item.path for item in attachments] == [
        f"/workspace/attachments/{second.id}.pdf",
        f"/workspace/attachments/{first.id}.pdf",
    ]


@pytest.mark.parametrize(
    "file_ids",
    [
        [str(uuid4())] * 6,
        ["not-a-uuid"],
    ],
)
def test_resolve_rejects_invalid_attachment_input(file_ids):
    with pytest.raises(ValidateErrorException, match="附件参数校验失败"):
        AgentAttachmentResolveReq.from_values(file_ids, owner_id=uuid4())


def test_resolve_rejects_duplicate_ids(session):
    account_id = uuid4()
    row = _upload(account_id)
    session.add(row)
    session.commit()

    with pytest.raises(ValidateErrorException, match="附件参数校验失败") as exc_info:
        AgentAttachmentResolveReq.from_values(
            [row.id, row.id],
            owner_id=account_id,
        )

    assert json.loads(json.dumps(exc_info.value.data))[0]["loc"] == ["file_ids"]


def test_resolve_rejects_cross_account_file(service, session):
    row = _upload(uuid4())
    session.add(row)
    session.commit()

    with pytest.raises(ForbiddenException, match="不属于当前用户"):
        service.resolve(
            AgentAttachmentResolveReq.from_values(
                [row.id],
                owner_id=uuid4(),
            )
        )


def test_materialize_downloads_and_uploads_in_submission_order(service):
    first = AgentAttachment(
        id=uuid4(),
        name="a.txt",
        key="a-key",
        url="https://files.example/a-key",
        size=1,
        extension="txt",
        mime_type="text/plain",
    )
    second = AgentAttachment(
        id=uuid4(),
        name="b.png",
        key="b-key",
        url="https://files.example/b-key",
        size=1,
        extension="png",
        mime_type="image/png",
    )
    downloaded = {"a-key": b"a", "b-key": b"b"}
    calls = []

    service.cos_service.download_file_bytes = lambda key: downloaded[key]

    class Backend:
        def upload_files(self, files):
            calls.extend(files)
            return [SimpleNamespace(error=None) for _ in files]

    service.materialize([first, second], Backend())

    assert [content for _, content in calls] == [b"a", b"b"]
    assert calls[0][0].endswith(f"{first.id}.txt")
    assert calls[1][0].endswith(f"{second.id}.png")


def test_attachment_path_rejects_unsafe_stored_extension():
    attachment = AgentAttachment(
        id=uuid4(),
        name="payload",
        key="payload-key",
        url="https://files.example/payload-key",
        size=1,
        extension="../../secret",
        mime_type="application/octet-stream",
    )

    assert attachment.extension == ""
    assert attachment.path == f"/workspace/attachments/{attachment.id}"


def test_materialize_rejects_incomplete_backend_response(service):
    attachment = AgentAttachment(
        id=uuid4(),
        name="a.txt",
        key="a-key",
        url="https://files.example/a-key",
        size=1,
        extension="txt",
        mime_type="text/plain",
    )
    service.cos_service.download_file_bytes = lambda _key: b"a"

    with pytest.raises(AgentAttachmentException, match="Workspace"):
        service.materialize(
            [attachment],
            SimpleNamespace(upload_files=lambda _files: []),
        )
