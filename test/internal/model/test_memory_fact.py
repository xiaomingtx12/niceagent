"""MemoryFact 模型单测。

约束 (硬约束 #5): 复用 SQLAlchemy, 不自建 ORM; 这里只测模型构造 + 序列化
+ 索引/约束定义正确, 不测 DB 实际行为 (DB 验证留给 alembic 链测试 + 集成)。

不依赖 DB 连接; 用直接构造 db.Model 实例的方式验证字段 / 约束定义。
"""
from datetime import datetime

from internal.model.memory_fact import MemoryFact


def _fact(**overrides):
    """构造一个 MemoryFact 实例 (不写 DB)."""
    defaults = dict(
        id=None,  # server_default 填
        account_id="acc-00000000-0000-0000-0000-000000000001",
        app_id="app-00000000-0000-0000-0000-000000000002",
        end_user_id=None,
        fact_type="preference",
        content="用户喜欢简短回复",
        importance=0.9,
        source_conversation_id=None,
        auto_extracted=True,
        deleted_at=None,
        created_at=datetime(2026, 7, 28),
        updated_at=datetime(2026, 7, 28),
    )
    defaults.update(overrides)
    return MemoryFact(**defaults)


# ---------- 字段 + 构造 ----------


def test_constructor_accepts_account_level_fact():
    """end_user_id=None → account 级 fact (creator / 全账号共享)."""
    f = _fact(end_user_id=None)
    assert f.end_user_id is None
    assert f.account_id is not None


def test_constructor_accepts_end_user_fact():
    """end_user_id 非 None → 端用户 fact, 只本人 + creator 可见."""
    f = _fact(end_user_id="eu-00000000-0000-0000-0000-000000000099")
    assert f.end_user_id == "eu-00000000-0000-0000-0000-000000000099"


def test_constructor_accepts_manual_fact():
    """auto_extracted=False → 用户/creator 手动写入 (e.g. 用户在 "我的记忆" 页面手填)."""
    f = _fact(auto_extracted=False)
    assert f.auto_extracted is False


def test_constructor_accepts_source_conversation_for_audit():
    """source_conversation_id 记录 LLM 抽取来源对话, audit / 回溯用."""
    f = _fact(
        source_conversation_id="cnv-00000000-0000-0000-0000-000000000003",
    )
    assert f.source_conversation_id == "cnv-00000000-0000-0000-0000-000000000003"


def test_constructor_accepts_soft_delete():
    """deleted_at 非 None → 软删除; 业务查询走 WHERE deleted_at IS NULL."""
    f = _fact(deleted_at=datetime(2026, 7, 28, 12, 0))
    assert f.deleted_at is not None


# ---------- 序列化 ----------


def test_to_dict_includes_required_fields():
    f = _fact()
    d = f.to_dict()
    assert "id" in d
    assert "account_id" in d
    assert "app_id" in d
    assert "end_user_id" in d
    assert d["fact_type"] == "preference"
    assert d["content"] == "用户喜欢简短回复"
    assert d["importance"] == 0.9
    assert d["confidence"] == 0.9
    assert d["auto_extracted"] is True
    assert d["created_at"] is not None
    assert d["updated_at"] is not None


def test_to_dict_handles_none_end_user_id():
    """end_user_id=None 时 to_dict 返回 None (前端区分 account 级 vs end-user 级)."""
    f = _fact(end_user_id=None)
    d = f.to_dict()
    assert d["end_user_id"] is None


def test_to_dict_serializes_uuid_as_string():
    """UUID 序列化成 str (JSON 兼容)."""
    f = _fact(
        account_id="00000000-0000-0000-0000-000000000001",
        app_id="00000000-0000-0000-0000-000000000002",
    )
    d = f.to_dict()
    assert isinstance(d["account_id"], str)
    assert isinstance(d["app_id"], str)


def test_to_dict_serializes_datetime_as_isoformat():
    """datetime 序列化成 ISO8601 (JSON 兼容)."""
    f = _fact(created_at=datetime(2026, 7, 28, 10, 30))
    d = f.to_dict()
    assert d["created_at"] == "2026-07-28T10:30:00"


# ---------- 约束 / 索引定义 ----------


def test_tablename_is_memory_facts():
    assert MemoryFact.__tablename__ == "memory_facts"


def test_has_importance_check_constraint():
    """importance 必须在 [0.0, 1.0]; SQLAlchemy 层 CheckConstraint 校验."""
    found = False
    for arg in MemoryFact.__table_args__:
        if arg.__class__.__name__ == "CheckConstraint":
            # TextClause 不可用 `if x` / `if not x` 判断 (会触发 __bool__);
            # 用 `is not None` + 访问 .text 属性取字符串
            sql_text = (
                arg.sqltext.text.lower() if arg.sqltext is not None else ""
            )
            if "importance" in sql_text:
                found = True
                break
    assert found, "expected importance range CheckConstraint"


def test_has_end_user_recall_index():
    """end-user '我的记忆' 列表 (account_id, app_id, end_user_id) 索引存在."""
    index_names = {arg.name for arg in MemoryFact.__table_args__ if hasattr(arg, "name")}
    assert "idx_memory_facts_end_user" in index_names


def test_has_recall_index_for_top_k():
    """召回排序 (account_id, app_id, importance, updated_at) 索引."""
    index_names = {arg.name for arg in MemoryFact.__table_args__ if hasattr(arg, "name")}
    assert "idx_memory_facts_recall" in index_names


def test_has_deleted_at_index():
    """deleted_at 索引支持 WHERE deleted_at IS NULL 默认过滤."""
    index_names = {arg.name for arg in MemoryFact.__table_args__ if hasattr(arg, "name")}
    assert "idx_memory_facts_deleted_at" in index_names


def test_importance_default_is_one():
    """importance 默认 1.0."""
    col = MemoryFact.__table__.columns["importance"]
    # server_default 在 columns definition 上
    assert col.server_default is not None
    assert "1.0" in str(col.server_default.arg)


def test_auto_extracted_default_is_true():
    """auto_extracted 默认 True (LLM 自动抽取占绝大多数, 手动写入业务显式置 False)."""
    col = MemoryFact.__table__.columns["auto_extracted"]
    assert col.server_default is not None
    assert "true" in str(col.server_default.arg).lower()
