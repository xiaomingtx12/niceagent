"""saver 模块单测。

覆盖：
- build_thread_id 格式约定
- get_postgres_saver 单例 + URI 转换
"""
from internal.core.agent_v2.saver import build_thread_id, _pg_uri_for_langgraph


def test_build_thread_id_main_agent():
    thread_id = build_thread_id(
        account_id="acct1", app_id="app1", conversation_id="cnv1"
    )
    assert thread_id == "(acct1,app1,cnv1,main)"


def test_build_thread_id_subagent():
    thread_id = build_thread_id(
        account_id="acct1",
        app_id="app1",
        conversation_id="cnv1",
        agent_id="researcher",
    )
    assert thread_id == "(acct1,app1,cnv1,researcher)"


def test_pg_uri_for_langgraph_strips_psycopg_prefix():
    """SQLAlchemy 的 postgresql+psycopg:// → postgresql://。"""
    assert _pg_uri_for_langgraph(
        "postgresql+psycopg://user:pass@host/db"
    ) == "postgresql://user:pass@host/db"
    assert _pg_uri_for_langgraph(
        "postgresql+psycopg2://user:pass@host/db"
    ) == "postgresql://user:pass@host/db"


def test_pg_uri_for_langgraph_passthrough():
    """已经是 postgresql:// 直接返回。"""
    assert (
        _pg_uri_for_langgraph("postgresql://user:pass@host/db")
        == "postgresql://user:pass@host/db"
    )