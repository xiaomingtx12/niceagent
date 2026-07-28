"""Final structured-memory service contract."""

from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from internal.exception import FailException
from internal.model.memory_fact import MemoryFact
from internal.service.memory_fact_service import MemoryFactService


@pytest.fixture
def db_session():
    import uuid as _uuid

    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _register(dbapi_conn, _record):
        dbapi_conn.create_function(
            "uuid_generate_v4",
            0,
            lambda: str(_uuid.uuid4()),
        )

    MemoryFact.__table__.create(engine, checkfirst=True)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def service(db_session):
    instance = MemoryFactService.__new__(MemoryFactService)
    instance.db = type("FakeDB", (), {"session": db_session})()
    return instance


def _fact(*, account_id, app_id, end_user_id, content, **overrides):
    values = {
        "id": uuid4(),
        "account_id": account_id,
        "app_id": app_id,
        "end_user_id": end_user_id,
        "fact_type": "general",
        "content": content,
        "importance": 0.8,
        "source_type": "auto",
        "auto_extracted": True,
    }
    values.update(overrides)
    return MemoryFact(**values)


def test_end_user_lists_only_own_active_facts(service, db_session):
    account_id, app_id, end_user_id = uuid4(), uuid4(), uuid4()
    db_session.add_all(
        [
            _fact(
                account_id=account_id,
                app_id=app_id,
                end_user_id=end_user_id,
                content="own",
            ),
            _fact(
                account_id=account_id,
                app_id=app_id,
                end_user_id=None,
                content="creator-debug",
            ),
            _fact(
                account_id=account_id,
                app_id=app_id,
                end_user_id=uuid4(),
                content="other-user",
            ),
            _fact(
                account_id=account_id,
                app_id=app_id,
                end_user_id=end_user_id,
                content="deleted",
                deleted_at=datetime(2026, 7, 28),
            ),
        ]
    )
    db_session.commit()

    rows = service.list_end_user_facts(
        account_id=account_id,
        app_id=app_id,
        end_user_id=end_user_id,
    )

    assert [row.content for row in rows] == ["own"]


def test_creator_lists_app_facts_read_only_and_can_filter_user(
    service,
    db_session,
):
    account_id, app_id, end_user_id = uuid4(), uuid4(), uuid4()
    db_session.add_all(
        [
            _fact(
                account_id=account_id,
                app_id=app_id,
                end_user_id=end_user_id,
                content="target",
            ),
            _fact(
                account_id=account_id,
                app_id=app_id,
                end_user_id=uuid4(),
                content="other",
            ),
            _fact(
                account_id=uuid4(),
                app_id=app_id,
                end_user_id=end_user_id,
                content="foreign-account",
            ),
        ]
    )
    db_session.commit()

    rows = service.list_creator_facts(
        account_id=account_id,
        app_id=app_id,
        end_user_id_filter=end_user_id,
    )

    assert [row.content for row in rows] == ["target"]


def test_end_user_create_and_update_recalculate_owned_fact(service):
    account_id, app_id, end_user_id = uuid4(), uuid4(), uuid4()
    row = service.create_end_user_fact(
        account_id=account_id,
        app_id=app_id,
        end_user_id=end_user_id,
        content="  中文回复  ",
        importance=0.7,
    )

    assert row.content == "中文回复"
    assert row.source_type == "manual"
    assert row.auto_extracted is False

    updated = service.update_end_user_fact(
        account_id=account_id,
        app_id=app_id,
        end_user_id=end_user_id,
        fact_id=row.id,
        content="简短中文回复",
        importance=0.9,
    )

    assert updated.content == "简短中文回复"
    assert updated.importance == 0.9
    assert updated.source_type == "edited"
    assert updated.auto_extracted is False


def test_end_user_cannot_mutate_another_users_fact(service, db_session):
    account_id, app_id = uuid4(), uuid4()
    row = _fact(
        account_id=account_id,
        app_id=app_id,
        end_user_id=uuid4(),
        content="private",
    )
    db_session.add(row)
    db_session.commit()

    with pytest.raises(FailException):
        service.update_end_user_fact(
            account_id=account_id,
            app_id=app_id,
            end_user_id=uuid4(),
            fact_id=row.id,
            content="tampered",
        )


def test_soft_and_batch_delete_are_scoped_to_owner(service, db_session):
    account_id, app_id, end_user_id = uuid4(), uuid4(), uuid4()
    own_a = _fact(
        account_id=account_id,
        app_id=app_id,
        end_user_id=end_user_id,
        content="a",
    )
    own_b = _fact(
        account_id=account_id,
        app_id=app_id,
        end_user_id=end_user_id,
        content="b",
    )
    other = _fact(
        account_id=account_id,
        app_id=app_id,
        end_user_id=uuid4(),
        content="other",
    )
    db_session.add_all([own_a, own_b, other])
    db_session.commit()

    service.soft_delete_end_user_fact(
        account_id=account_id,
        app_id=app_id,
        end_user_id=end_user_id,
        fact_id=own_a.id,
    )
    count = service.batch_delete_end_user_facts(
        account_id=account_id,
        app_id=app_id,
        end_user_id=end_user_id,
    )

    assert count == 1
    assert own_a.deleted_at is not None
    assert own_b.deleted_at is not None
    assert other.deleted_at is None


def test_auto_fact_threshold_and_deduplication(service):
    account_id, app_id, end_user_id = uuid4(), uuid4(), uuid4()
    assert (
        service.create_auto_fact(
            account_id=account_id,
            app_id=app_id,
            end_user_id=end_user_id,
            content="low value",
            importance=0.59,
        )
        is None
    )

    first = service.create_auto_fact(
        account_id=account_id,
        app_id=app_id,
        end_user_id=end_user_id,
        content="uses PostgreSQL 16",
        importance=0.7,
    )
    duplicate = service.create_auto_fact(
        account_id=account_id,
        app_id=app_id,
        end_user_id=end_user_id,
        content="uses PostgreSQL 16",
        importance=0.9,
    )

    assert duplicate.id == first.id
    assert duplicate.importance == 0.9


def test_creator_debug_auto_fact_uses_null_end_user_scope(service):
    row = service.create_auto_fact(
        account_id=uuid4(),
        app_id=uuid4(),
        end_user_id=None,
        content="creator debug preference",
        importance=0.8,
    )

    assert row is not None
    assert row.end_user_id is None


def test_recall_uses_keyword_filter_then_importance(
    service,
    db_session,
):
    account_id, app_id, end_user_id = uuid4(), uuid4(), uuid4()
    db_session.add_all(
        [
            _fact(
                account_id=account_id,
                app_id=app_id,
                end_user_id=end_user_id,
                content="prefers concise Chinese answers",
                importance=0.6,
            ),
            _fact(
                account_id=account_id,
                app_id=app_id,
                end_user_id=end_user_id,
                content="prefers detailed English answers",
                importance=1.0,
            ),
            _fact(
                account_id=account_id,
                app_id=app_id,
                end_user_id=uuid4(),
                content="foreign",
                importance=1.0,
            ),
        ]
    )
    db_session.commit()

    rows = service.recall_top_k(
        account_id=account_id,
        app_id=app_id,
        end_user_id=end_user_id,
        keyword="Chinese",
        k=2,
    )

    assert [row.content for row in rows] == ["prefers concise Chinese answers"]


def test_creator_debug_recall_uses_only_null_end_user(service, db_session):
    account_id, app_id = uuid4(), uuid4()
    db_session.add_all(
        [
            _fact(
                account_id=account_id,
                app_id=app_id,
                end_user_id=None,
                content="creator-debug",
            ),
            _fact(
                account_id=account_id,
                app_id=app_id,
                end_user_id=uuid4(),
                content="end-user-private",
            ),
        ]
    )
    db_session.commit()

    rows = service.recall_top_k(
        account_id=account_id,
        app_id=app_id,
        end_user_id=None,
        k=5,
    )

    assert [row.content for row in rows] == ["creator-debug"]
