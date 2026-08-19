"""第三方模型动态发现测试。"""

import requests
from uuid import uuid4

from internal.core.language_model.entities.model_entity import ModelFeature
from internal.core.language_model.entities.model_entity import ModelEntity
from internal.core.language_model.entities.provider_entity import (
    ModelDiscoveryConfig,
    ProviderEntity,
)
from internal.core.language_model.language_model_manager import LanguageModelManager
from internal.core.language_model.model_discovery import ModelDiscoveryClient
from internal.core.language_model.providers.openai.chat import Chat
from internal.service.language_model_service import LanguageModelService


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def test_discover_parses_openai_models_and_uses_cache(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _Response(
            {
                "data": [
                    {
                        "id": "vendor-chat-1",
                        "owned_by": "vendor",
                        "context_length": 32768,
                    },
                    {
                        "id": "vendor-no-tools",
                        "capabilities": {"tools": False},
                    },
                ]
            }
        )

    monkeypatch.setattr(
        "internal.core.language_model.model_discovery.requests.get",
        fake_get,
    )
    provider = ProviderEntity(
        name="vendor",
        base_url="https://vendor.example/v1",
        model_discovery=ModelDiscoveryConfig(enabled=True),
    )

    client = ModelDiscoveryClient()
    models = client.discover(provider, "secret")
    cached_models = client.discover(provider, "secret")

    assert [model.model_name for model in models] == [
        "vendor-chat-1",
        "vendor-no-tools",
    ]
    assert models[0].is_dynamic is True
    assert models[0].context_window == 32768
    assert models[0].attributes["base_url"] == "https://vendor.example/v1"
    assert ModelFeature.TOOL_CALL not in cached_models[1].features
    assert len(calls) == 1
    assert calls[0][0] == "https://vendor.example/v1/models"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer secret"


def test_discover_falls_back_to_previous_success(monkeypatch):
    responses = [
        _Response({"models": [{"name": "vendor-chat"}]}),
        requests.Timeout("temporary outage"),
    ]

    def fake_get(*args, **kwargs):
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(
        "internal.core.language_model.model_discovery.requests.get",
        fake_get,
    )
    provider = ProviderEntity(
        name="vendor",
        base_url="https://vendor.example/v1",
        model_discovery=ModelDiscoveryConfig(
            enabled=True,
            fallback_endpoints=[],
        ),
    )
    client = ModelDiscoveryClient()

    first = client.discover(provider, "secret")
    second = client.discover(provider, "secret", force_refresh=True)

    assert [model.model_name for model in first] == ["vendor-chat"]
    assert [model.model_name for model in second] == ["vendor-chat"]


def test_manager_merges_discovered_model_and_uses_generic_chat_class(monkeypatch):
    manager = LanguageModelManager()
    dynamic_model = ModelEntity(
        model="vendor-chat",
        label="Vendor Chat",
        is_dynamic=True,
        attributes={
            "model": "vendor-chat",
            "base_url": "https://vendor.example/v1",
        },
    )
    monkeypatch.setattr(
        manager._discovery_client,
        "discover",
        lambda provider, api_key, force_refresh=False: [dynamic_model],
    )

    models = manager.get_model_entities("openai", api_key="secret")
    resolved = manager.get_model_entity(
        "openai",
        "vendor-chat",
        api_key="secret",
    )
    model_class = manager.get_provider("openai").get_model_class(
        resolved.model_type,
        model_entity=resolved,
    )

    assert "vendor-chat" in {model.model_name for model in models}
    assert resolved.is_dynamic is True
    assert model_class is Chat


def test_language_model_service_loads_discovered_model(monkeypatch):
    manager = LanguageModelManager()
    dynamic_model = ModelEntity(
        model="vendor-chat",
        is_dynamic=True,
        attributes={
            "model": "vendor-chat",
            "base_url": "https://vendor.example/v1",
        },
    )
    monkeypatch.setattr(
        manager._discovery_client,
        "discover",
        lambda provider, api_key, force_refresh=False: [dynamic_model],
    )
    monkeypatch.setattr(
        "internal.service.language_model_service.get_api_key_for_provider",
        lambda provider_name, account_id: "secret",
    )

    service = LanguageModelService(
        db=None,
        language_model_manager=manager,
    )
    model = service.load_language_model(
        {
            "provider": "openai",
            "model": "vendor-chat",
            "parameters": {},
        },
        account_id=uuid4(),
    )

    assert model.model_name == "vendor-chat"
    assert model.openai_api_base == "https://vendor.example/v1"
