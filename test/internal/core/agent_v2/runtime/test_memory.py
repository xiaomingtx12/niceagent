import pytest
from langgraph.store.memory import InMemoryStore

from internal.core.agent_v2.runtime.memory import MemoryContextAssembler


def test_memory_context_requires_a_store():
    assembler = MemoryContextAssembler(store_provider=lambda: None)

    with pytest.raises(RuntimeError, match="无记忆模式"):
        assembler.build()


def test_memory_context_always_exposes_memories_route():
    store = InMemoryStore()
    projection = MemoryContextAssembler(
        store_provider=lambda: store,
    ).build()

    assert projection.store is store
    assert projection.sources == ["/memories/AGENTS.md"]
    assert "/memories/" in projection.routes
