from types import SimpleNamespace
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy import MetaData
from sqlalchemy.orm import sessionmaker

from internal.exception import FailException, ForbiddenException
from internal.model import UploadFile
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
        cos_service=SimpleNamespace(),
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
        [str(second.id), first.id],
        owner_id=account_id,
    )

    assert [item.id for item in attachments] == [second.id, first.id]
    assert [item.path for item in attachments] == [
        f"/workspace/attachments/{second.id}.pdf",
        f"/workspace/attachments/{first.id}.pdf",
    ]


@pytest.mark.parametrize(
    ("file_ids", "message"),
    [
        ([str(uuid4())] * 6, "最多只能携带"),
        (["not-a-uuid"], "合法 UUID"),
    ],
)
def test_resolve_rejects_invalid_attachment_input(service, file_ids, message):
    with pytest.raises(FailException, match=message):
        service.resolve(file_ids, owner_id=uuid4())


def test_resolve_rejects_duplicate_ids(service, session):
    account_id = uuid4()
    row = _upload(account_id)
    session.add(row)
    session.commit()

    with pytest.raises(FailException, match="不能重复"):
        service.resolve([row.id, row.id], owner_id=account_id)


def test_resolve_rejects_cross_account_file(service, session):
    row = _upload(uuid4())
    session.add(row)
    session.commit()

    with pytest.raises(ForbiddenException, match="不属于当前用户"):
        service.resolve([row.id], owner_id=uuid4())


def test_materialize_downloads_and_uploads_in_submission_order(service):
    from internal.service.agent_attachment_service import AgentAttachment

    first = AgentAttachment(
        id=uuid4(),
        name="a.txt",
        key="a-key",
        size=1,
        extension="txt",
        mime_type="text/plain",
    )
    second = AgentAttachment(
        id=uuid4(),
        name="b.png",
        key="b-key",
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
