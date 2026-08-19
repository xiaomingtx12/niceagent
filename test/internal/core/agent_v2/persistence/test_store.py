from types import SimpleNamespace

import pytest
from langgraph.store.memory import InMemoryStore

from internal.core.agent_v2.persistence.store import StoreProvider


def test_configured_memory_store_is_reused():
    provider = StoreProvider(
        config_factory=lambda: SimpleNamespace(AGENT_MEMORY_BACKEND="memory"),
        register_exit=lambda _callback: None,
    )

    first = provider.get_configured_store()
    second = provider.get_configured_store()

    assert isinstance(first, InMemoryStore)
    assert second is first


@pytest.mark.parametrize("mode", ["disabled", "false", "off", "none"])
def test_store_provider_rejects_disabled_mode(mode):
    provider = StoreProvider(
        config_factory=lambda: SimpleNamespace(AGENT_MEMORY_BACKEND=mode),
    )

    with pytest.raises(ValueError, match="不允许关闭"):
        provider.get_configured_store()
