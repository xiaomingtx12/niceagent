"""MemoryFactService 单测。

约束（硬约束 #5）：
- 复用 master BaseService / SQLAlchemy / db.Model
- 不连真实 DB；用 in-memory SQLite 跑 schema + service
- 覆盖可见性矩阵：end-user 本人 / creator 全 app / 跨 account 403 / 软删除

依赖：
- SQLAlchemy 需要 sqlite3 + master db.Model 注册；用 `internal.extension.database_extension.db`
  创建 in-memory engine + create_all()
"""
from datetime import datetime
from uuid import uuid4

import pytest

from internal.model.memory_fact import MemoryFact


@pytest.fixture
def db_session():
    """in-memory SQLite + 自注册 uuid_generate_v4 (PG 兼容函数)."""
    import uuid as _uuid
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _register(dbapi_conn, _record):
        dbapi_conn.create_function(
            "uuid_generate_v4", 0, lambda: str(_uuid.uuid4())
        )

    MemoryFact.__table__.create(engine, checkfirst=True)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def service(db_session):
    from internal.service.memory_fact_service import MemoryFactService
    # 让 service.db.session 等于 db_session (生产走 injector 注入 SQLAlchemy)
    svc = MemoryFactService.__new__(MemoryFactService)
    svc.db = type("FakeDB", (), {"session": db_session})()
    return svc


# 通用 fixture
def _fact(**overrides):
    defaults = dict(
        id=None,
        account_id=uuid4(),
        app_id=uuid4(),
        end_user_id=uuid4(),
        fact_type="preference",
        content="default",
        confidence=0.9,
        auto_extracted=True,
        deleted_at=None,
    )
    defaults.update(overrides)
    defaults["id"] = defaults["id"] or uuid4()
    return MemoryFact(**defaults)


# ---------- list_end_user_facts ----------


def test_list_end_user_returns_own_and_account_level(service, db_session):
    """end-user 看本人 + account 级 (end_user_id IS NULL) fact."""
    own = _fact(end_user_id=None)  # placeholder; override below
    own_id = uuid4()
    other_eu = uuid4()
    account_id = uuid4()
    app_id = uuid4()
    end_user_id = uuid4()

    db_session.add(MemoryFact(
        id=own_id, account_id=account_id, app_id=app_id,
        end_user_id=end_user_id, fact_type="preference",
        content="本人 fact", confidence=0.9,
    ))
    db_session.add(MemoryFact(
        id=uuid4(), account_id=account_id, app_id=app_id,
        end_user_id=None, fact_type="profile",
        content="account 级 fact", confidence=1.0,
    ))
    db_session.add(MemoryFact(
        id=uuid4(), account_id=account_id, app_id=app_id,
        end_user_id=other_eu, fact_type="preference",
        content="他人 fact, 不该出现", confidence=0.9,
    ))
    db_session.commit()

    out = service.list_end_user_facts(
        account_id=str(account_id), app_id=str(app_id),
        end_user_id=str(end_user_id),
    )
    contents = {f.content for f in out}
    assert "本人 fact" in contents
    assert "account 级 fact" in contents
    assert "他人 fact, 不该出现" not in contents


def test_list_end_user_excludes_soft_deleted(service, db_session):
    """soft-deleted fact 不出现 (WHERE deleted_at IS NULL)."""
    account_id, app_id, end_user_id = uuid4(), uuid4(), uuid4()
    db_session.add(MemoryFact(
        id=uuid4(), account_id=account_id, app_id=app_id,
        end_user_id=end_user_id, fact_type="preference",
        content="alive", confidence=0.9,
    ))
    db_session.add(MemoryFact(
        id=uuid4(), account_id=account_id, app_id=app_id,
        end_user_id=end_user_id, fact_type="preference",
        content="deleted", confidence=0.9,
        deleted_at=datetime(2026, 7, 28),
    ))
    db_session.commit()

    out = service.list_end_user_facts(
        account_id=str(account_id), app_id=str(app_id),
        end_user_id=str(end_user_id),
    )
    contents = {f.content for f in out}
    assert "alive" in contents
    assert "deleted" not in contents


def test_list_end_user_isolates_account(service, db_session):
    """跨 account 看不到 (硬隔离)."""
    acc_a, acc_b = uuid4(), uuid4()
    app_id = uuid4()
    end_user_id = uuid4()
    db_session.add(MemoryFact(
        id=uuid4(), account_id=acc_a, app_id=app_id,
        end_user_id=end_user_id, fact_type="preference",
        content="acc A fact", confidence=0.9,
    ))
    db_session.commit()

    out = service.list_end_user_facts(
        account_id=str(acc_b), app_id=str(app_id),
        end_user_id=str(end_user_id),
    )
    assert out == []


# ---------- list_creator_facts ----------


def test_list_creator_returns_all_end_users_in_app(service, db_session):
    """creator 看全 app, 不分 end_user."""
    account_id, app_id = uuid4(), uuid4()
    eu1, eu2 = uuid4(), uuid4()
    db_session.add(MemoryFact(
        id=uuid4(), account_id=account_id, app_id=app_id,
        end_user_id=eu1, fact_type="preference",
        content="eu1 fact", confidence=0.9,
    ))
    db_session.add(MemoryFact(
        id=uuid4(), account_id=account_id, app_id=app_id,
        end_user_id=eu2, fact_type="preference",
        content="eu2 fact", confidence=0.9,
    ))
    db_session.add(MemoryFact(
        id=uuid4(), account_id=account_id, app_id=app_id,
        end_user_id=None, fact_type="profile",
        content="account level", confidence=1.0,
    ))
    db_session.commit()

    out = service.list_creator_facts(
        account_id=str(account_id), app_id=str(app_id),
    )
    contents = {f.content for f in out}
    assert contents == {"eu1 fact", "eu2 fact", "account level"}


def test_list_creator_filters_account_level_only(service, db_session):
    """creator 面板 'account 级' 过滤器: end_user_id_filter=''."""
    account_id, app_id = uuid4(), uuid4()
    eu = uuid4()
    db_session.add(MemoryFact(
        id=uuid4(), account_id=account_id, app_id=app_id,
        end_user_id=eu, fact_type="preference",
        content="end-user fact", confidence=0.9,
    ))
    db_session.add(MemoryFact(
        id=uuid4(), account_id=account_id, app_id=app_id,
        end_user_id=None, fact_type="profile",
        content="account only", confidence=1.0,
    ))
    db_session.commit()

    out = service.list_creator_facts(
        account_id=str(account_id), app_id=str(app_id),
        end_user_id_filter="",
    )
    contents = {f.content for f in out}
    assert contents == {"account only"}


def test_list_creator_filters_by_fact_type(service, db_session):
    account_id, app_id = uuid4(), uuid4()
    db_session.add(MemoryFact(
        id=uuid4(), account_id=account_id, app_id=app_id,
        end_user_id=None, fact_type="preference", content="p", confidence=0.9,
    ))
    db_session.add(MemoryFact(
        id=uuid4(), account_id=account_id, app_id=app_id,
        end_user_id=None, fact_type="instruction", content="i", confidence=0.9,
    ))
    db_session.commit()

    out = service.list_creator_facts(
        account_id=str(account_id), app_id=str(app_id),
        fact_type_filter="instruction",
    )
    assert [f.content for f in out] == ["i"]


# ---------- create_fact ----------


def test_create_fact_end_user_manual(service, db_session):
    """end-user 手动写入 (auto_extracted=False), end_user_id 必填."""
    f = service.create_fact(
        account_id=str(uuid4()), app_id=str(uuid4()),
        end_user_id=str(uuid4()), fact_type="preference",
        content="我喜欢简短回答", auto_extracted=False,
    )
    assert f.id is not None
    assert f.auto_extracted is False
    assert f.content == "我喜欢简短回答"


def test_create_fact_creator_account_level(service, db_session):
    """creator 写入 account 级 fact (end_user_id=None)."""
    f = service.create_fact(
        account_id=str(uuid4()), app_id=str(uuid4()),
        end_user_id=None, fact_type="profile",
        content="本 app 是销售助手",
    )
    assert f.end_user_id is None
    assert f.auto_extracted is False


def test_create_fact_empty_content_raises(service, db_session):
    from internal.exception import FailException
    with pytest.raises(FailException):
        service.create_fact(
            account_id=str(uuid4()), app_id=str(uuid4()),
            end_user_id=str(uuid4()), fact_type="preference",
            content="   ",
        )


def test_create_fact_invalid_confidence_raises(service, db_session):
    from internal.exception import FailException
    with pytest.raises(FailException):
        service.create_fact(
            account_id=str(uuid4()), app_id=str(uuid4()),
            end_user_id=str(uuid4()), fact_type="preference",
            content="x", confidence=1.5,
        )


def test_create_auto_fact_requires_end_user(service, db_session):
    """auto_extracted=True 时 end_user_id 必填 (LLM 抽取归属某个端用户)."""
    from internal.exception import FailException
    with pytest.raises(FailException):
        service.create_fact(
            account_id=str(uuid4()), app_id=str(uuid4()),
            end_user_id=None, fact_type="preference",
            content="x", auto_extracted=True,
        )


# ---------- soft_delete_fact ----------


def test_end_user_can_delete_own_fact(service, db_session):
    """end-user 删本人 fact -> deleted_at 设置."""
    fact_id = uuid4()
    end_user_id = uuid4()
    account_id = uuid4()
    db_session.add(MemoryFact(
        id=fact_id, account_id=account_id, app_id=uuid4(),
        end_user_id=end_user_id, fact_type="preference",
        content="x", confidence=0.9,
    ))
    db_session.commit()

    out = service.soft_delete_fact(
        account_id=str(account_id), fact_id=fact_id,
        actor_end_user_id=str(end_user_id), actor_is_creator=False,
    )
    assert out.deleted_at is not None


def test_end_user_cannot_delete_other_users_fact(service, db_session):
    """end-user 删他人 fact -> 403."""
    from internal.exception import FailException
    fact_id = uuid4()
    db_session.add(MemoryFact(
        id=fact_id, account_id=uuid4(), app_id=uuid4(),
        end_user_id=uuid4(), fact_type="preference",
        content="x", confidence=0.9,
    ))
    db_session.commit()

    with pytest.raises(FailException):
        service.soft_delete_fact(
            account_id=str(uuid4()), fact_id=fact_id,
            actor_end_user_id=str(uuid4()), actor_is_creator=False,
        )


def test_end_user_cannot_delete_account_level_fact(service, db_session):
    """end-user 删 account 级 fact -> 403 (保护 creator 写入的全局偏好)."""
    from internal.exception import FailException
    fact_id = uuid4()
    account_id = uuid4()
    end_user_id = uuid4()
    db_session.add(MemoryFact(
        id=fact_id, account_id=account_id, app_id=uuid4(),
        end_user_id=None, fact_type="profile", content="x", confidence=1.0,
    ))
    db_session.commit()

    with pytest.raises(FailException):
        service.soft_delete_fact(
            account_id=str(account_id), fact_id=fact_id,
            actor_end_user_id=str(end_user_id), actor_is_creator=False,
        )


def test_creator_can_delete_any_fact(service, db_session):
    """creator 可删本 account 任何 fact (含他人 end-user fact)."""
    fact_id = uuid4()
    account_id = uuid4()
    db_session.add(MemoryFact(
        id=fact_id, account_id=account_id, app_id=uuid4(),
        end_user_id=uuid4(), fact_type="preference",
        content="x", confidence=0.9,
    ))
    db_session.commit()

    out = service.soft_delete_fact(
        account_id=str(account_id), fact_id=fact_id,
        actor_is_creator=True,
    )
    assert out.deleted_at is not None


def test_soft_delete_isolates_account(service, db_session):
    """跨 account 删 -> 403 (找不到 or forbidden)."""
    from internal.exception import FailException
    fact_id = uuid4()
    db_session.add(MemoryFact(
        id=fact_id, account_id=uuid4(), app_id=uuid4(),
        end_user_id=uuid4(), fact_type="preference",
        content="x", confidence=0.9,
    ))
    db_session.commit()

    with pytest.raises(FailException):
        # 用不同的 account_id, 即使 actor_is_creator=True 也找不到
        service.soft_delete_fact(
            account_id=str(uuid4()), fact_id=fact_id,
            actor_is_creator=True,
        )


def test_soft_delete_twice_raises(service, db_session):
    """已删除 fact 再次删除 -> FailException (避免重置 deleted_at)."""
    from internal.exception import FailException
    fact_id = uuid4()
    account_id = uuid4()
    db_session.add(MemoryFact(
        id=fact_id, account_id=account_id, app_id=uuid4(),
        end_user_id=uuid4(), fact_type="preference",
        content="x", confidence=0.9,
        deleted_at=datetime(2026, 7, 28),
    ))
    db_session.commit()

    with pytest.raises(FailException):
        service.soft_delete_fact(
            account_id=str(account_id), fact_id=fact_id,
            actor_is_creator=True,
        )


# ---------- recall_top_k ----------


def test_recall_returns_own_plus_account_level(service, db_session):
    """end-user 召回: 本人 fact + account 级 fact."""
    account_id, app_id, eu = uuid4(), uuid4(), uuid4()
    other_eu = uuid4()
    db_session.add(MemoryFact(
        id=uuid4(), account_id=account_id, app_id=app_id,
        end_user_id=eu, fact_type="preference",
        content="own", confidence=0.9,
    ))
    db_session.add(MemoryFact(
        id=uuid4(), account_id=account_id, app_id=app_id,
        end_user_id=None, fact_type="profile",
        content="account", confidence=1.0,
    ))
    db_session.add(MemoryFact(
        id=uuid4(), account_id=account_id, app_id=app_id,
        end_user_id=other_eu, fact_type="preference",
        content="other", confidence=0.9,
    ))
    db_session.commit()

    out = service.recall_top_k(
        account_id=str(account_id), app_id=str(app_id),
        end_user_id=str(eu), k=10,
    )
    contents = {f.content for f in out}
    assert "own" in contents
    assert "account" in contents
    assert "other" not in contents


def test_recall_creator_only_account_level(service, db_session):
    """end_user_id=None → 只召回 account 级 (creator 调试场景)."""
    account_id, app_id, eu = uuid4(), uuid4(), uuid4()
    db_session.add(MemoryFact(
        id=uuid4(), account_id=account_id, app_id=app_id,
        end_user_id=eu, fact_type="preference",
        content="eu", confidence=0.9,
    ))
    db_session.add(MemoryFact(
        id=uuid4(), account_id=account_id, app_id=app_id,
        end_user_id=None, fact_type="profile",
        content="account", confidence=1.0,
    ))
    db_session.commit()

    out = service.recall_top_k(
        account_id=str(account_id), app_id=str(app_id),
        end_user_id=None, k=10,
    )
    contents = {f.content for f in out}
    assert contents == {"account"}


def test_recall_top_k_limits_and_orders_by_confidence(service, db_session):
    """recall 按 (confidence desc, updated_at desc) 排序, 取 top-K."""
    account_id, app_id, eu = uuid4(), uuid4(), uuid4()
    for i, conf in enumerate([0.5, 0.9, 0.7]):
        db_session.add(MemoryFact(
            id=uuid4(), account_id=account_id, app_id=app_id,
            end_user_id=eu, fact_type="preference",
            content=f"f{i}", confidence=conf,
            updated_at=datetime(2026, 7, i + 1),
        ))
    db_session.commit()

    out = service.recall_top_k(
        account_id=str(account_id), app_id=str(app_id),
        end_user_id=str(eu), k=2,
    )
    # confidence 0.9, 0.7
    assert [f.content for f in out] == ["f1", "f2"]


def test_recall_excludes_soft_deleted(service, db_session):
    account_id, app_id, eu = uuid4(), uuid4(), uuid4()
    db_session.add(MemoryFact(
        id=uuid4(), account_id=account_id, app_id=app_id,
        end_user_id=eu, fact_type="preference",
        content="alive", confidence=0.9,
    ))
    db_session.add(MemoryFact(
        id=uuid4(), account_id=account_id, app_id=app_id,
        end_user_id=eu, fact_type="preference",
        content="deleted", confidence=1.0,
        deleted_at=datetime(2026, 7, 28),
    ))
    db_session.commit()

    out = service.recall_top_k(
        account_id=str(account_id), app_id=str(app_id),
        end_user_id=str(eu), k=10,
    )
    contents = {f.content for f in out}
    assert "alive" in contents
    assert "deleted" not in contents


def test_recall_empty_when_no_facts(service, db_session):
    out = service.recall_top_k(
        account_id=str(uuid4()), app_id=str(uuid4()),
        end_user_id=str(uuid4()), k=10,
    )
    assert out == []