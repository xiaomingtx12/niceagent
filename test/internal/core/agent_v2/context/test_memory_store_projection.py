"""DeepAgents StoreBackend 记忆投影测试。"""

from types import SimpleNamespace
from uuid import uuid4

from langgraph.store.memory import InMemoryStore

from internal.core.agent_v2.context.memory import MemoryContextAssembler
from internal.core.agent_v2.contract import AgentRunContext


def _context(*, end_user_id):
    return AgentRunContext(
        account_id=uuid4(),
        app_id=uuid4(),
        conversation_id=uuid4(),
        created_by=uuid4(),
        end_user_id=end_user_id,
        query="偏好",
    )


def test_memory_projection_uses_scoped_store_backend():
    store = InMemoryStore()
    service = SimpleNamespace(
        recall_top_k=lambda **_kwargs: [
            SimpleNamespace(fact_type="preference", content="中文")
        ]
    )
    assembler = MemoryContextAssembler(
        service,
        store_provider=lambda: store,
    )
    run_context = _context(end_user_id=uuid4())

    projection = assembler.build(
        run_context=run_context,
        settings=SimpleNamespace(AGENT_MEMORY_RECALL_K=5),
    )

    assert projection.sources == ["/memories/AGENTS.md"]
    assert projection.store is store
    assert projection.namespace == (
        "mydify",
        str(run_context.account_id),
        str(run_context.app_id),
        str(run_context.end_user_id),
    )
    read = projection.routes["/memories/"].read("/AGENTS.md")
    assert read.error is None
    assert "中文" in read.file_data["content"]

    stored = store.get(projection.namespace, "/AGENTS.md")
    assert stored is not None
    assert "中文" in stored.value["content"]


def test_memory_projection_namespaces_account_scope_when_end_user_missing():
    store = InMemoryStore()
    service = SimpleNamespace(
        recall_top_k=lambda **_kwargs: [
            SimpleNamespace(fact_type="project", content="仅账号可见")
        ]
    )
    projection = MemoryContextAssembler(
        service,
        store_provider=lambda: store,
    ).build(
        run_context=_context(end_user_id=None),
        settings=SimpleNamespace(AGENT_MEMORY_RECALL_K=5),
    )

    assert projection.namespace[-1] == "account"
    assert store.get(projection.namespace, "/AGENTS.md") is not None
