import pytest

from config.config import Config


def test_memory_backend_defaults_to_postgres(monkeypatch):
    monkeypatch.delenv("AGENT_MEMORY_BACKEND", raising=False)

    assert Config().AGENT_MEMORY_BACKEND == "postgres"


@pytest.mark.parametrize("value", ["disabled", "false", "off", "none"])
def test_memory_backend_cannot_be_disabled(monkeypatch, value):
    monkeypatch.setenv("AGENT_MEMORY_BACKEND", value)

    with pytest.raises(ValueError, match="AGENT_MEMORY_BACKEND"):
        Config()
