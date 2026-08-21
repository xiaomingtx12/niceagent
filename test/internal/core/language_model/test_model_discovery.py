"""第三方模型动态发现测试。"""

import requests
from uuid import uuid4

from internal.core.language_model.entities.model_entity import (
    ModelEntity,
    ModelModality,
    ModelType,
)
from internal.core.language_model.entities.provider_entity import (
    ModelDiscoveryConfig,
    ProviderEntity,
)
from internal.core.language_model.language_model_manager import LanguageModelManager
from internal.core.language_model.model_discovery import ModelDiscoveryClient
from internal.core.language_model.providers.openai.chat import Chat
from internal.service.language_model_service import LanguageModelService
from internal.service.provider_api_key_service import ProviderApiKeyService


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
                        "architecture": {
                            "input_modalities": ["text", "image", "audio"]
                        },
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
    assert models[0].input_modalities == [
        ModelModality.TEXT,
        ModelModality.IMAGE,
        ModelModality.AUDIO,
    ]
    assert models[0].output_modalities == [ModelModality.TEXT]
    assert models[0].attributes["base_url"] == "https://vendor.example/v1"
    assert cached_models[1].features == []
    assert models[0].metadata["input_modalities_source"] == "declared"
    assert len(calls) == 1
    assert calls[0][0] == "https://vendor.example/v1/models"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer secret"


def test_discover_uses_custom_relay_url(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return _Response({"data": [{"id": "relay-chat"}]})

    monkeypatch.setattr(
        "internal.core.language_model.model_discovery.requests.get",
        fake_get,
    )
    provider = ProviderEntity(
        name="vendor",
        base_url="https://vendor.example/v1",
        model_discovery=ModelDiscoveryConfig(enabled=True),
    )

    models = ModelDiscoveryClient().discover(
        provider,
        "secret",
        base_url="https://relay.example/v1/",
    )

    assert calls == ["https://relay.example/v1/models"]
    assert models[0].attributes["base_url"] == "https://relay.example/v1"


def test_discover_infers_visual_for_gpt5_alias_without_metadata():
    provider = ProviderEntity(
        name="vendor",
        base_url="https://vendor.example/v1",
        model_discovery=ModelDiscoveryConfig(enabled=True),
    )

    model = ModelDiscoveryClient()._build_model_entity(
        provider,
        {"id": "gpt5.6sol"},
    )

    assert model is not None
    assert model.input_modalities == [ModelModality.TEXT, ModelModality.IMAGE]
    assert model.output_modalities == [ModelModality.TEXT]
    assert model.metadata["input_modalities_source"] == "inferred"


def test_declared_modalities_override_name_inference():
    provider = ProviderEntity(
        name="vendor",
        base_url="https://vendor.example/v1",
        model_discovery=ModelDiscoveryConfig(enabled=True),
    )

    model = ModelDiscoveryClient()._build_model_entity(
        provider,
        {"id": "gpt5-text-proxy", "input_modalities": ["text"]},
    )

    assert model is not None
    assert model.input_modalities == [ModelModality.TEXT]
    assert model.metadata["input_modalities_source"] == "declared"


def test_manual_capabilities_survive_snapshot_normalization():
    models = ProviderApiKeyService._normalize_discovered_models(
        [
            {
                "model": "relay-model",
                "label": "Relay Model",
                "model_type": "chat",
                "input_modalities": ["text", "image"],
                "output_modalities": ["text", "audio"],
                "native_file_input": True,
                "metadata": {
                    "model_type_source": "manual",
                    "input_modalities_source": "manual",
                    "output_modalities_source": "manual",
                },
            }
        ]
    )

    assert models[0]["model_type"] == "chat"
    assert models[0]["input_modalities"] == ["text", "image"]
    assert models[0]["output_modalities"] == ["text", "audio"]
    assert models[0]["native_file_input"] is True
    assert models[0]["metadata"]["model_type_source"] == "manual"


def test_old_modalities_snapshot_migrates_file_to_native_file_input():
    models = ProviderApiKeyService._normalize_discovered_models(
        [{"model": "legacy-model", "modalities": ["text", "image", "file"]}]
    )

    assert models[0]["input_modalities"] == ["text", "image"]
    assert models[0]["native_file_input"] is True
    assert "modalities" not in models[0]


def test_discovery_classifies_specialized_model_roles():
    provider = ProviderEntity(
        name="vendor",
        base_url="https://vendor.example/v1",
        model_discovery=ModelDiscoveryConfig(enabled=True),
    )
    client = ModelDiscoveryClient()

    cases = {
        "text-embedding-3-large": (
            ModelType.EMBEDDING,
            [ModelModality.TEXT],
            [ModelModality.VECTOR],
        ),
        "bge-reranker-v2": (
            ModelType.RERANK,
            [ModelModality.TEXT],
            [ModelModality.RANKING],
        ),
        "gpt-image-1": (
            ModelType.IMAGE_GENERATION,
            [ModelModality.TEXT],
            [ModelModality.IMAGE],
        ),
        "sora-2": (
            ModelType.VIDEO_GENERATION,
            [ModelModality.TEXT],
            [ModelModality.VIDEO],
        ),
        "whisper-1": (
            ModelType.SPEECH_TO_TEXT,
            [ModelModality.AUDIO],
            [ModelModality.TEXT],
        ),
        "tts-1": (
            ModelType.TEXT_TO_SPEECH,
            [ModelModality.TEXT],
            [ModelModality.AUDIO],
        ),
    }

    for model_name, expected in cases.items():
        model = client._build_model_entity(provider, {"id": model_name})
        assert model is not None
        assert (model.model_type, model.input_modalities, model.output_modalities) == expected


def test_declared_model_role_and_output_override_name_inference():
    provider = ProviderEntity(
        name="vendor",
        base_url="https://vendor.example/v1",
        model_discovery=ModelDiscoveryConfig(enabled=True),
    )

    model = ModelDiscoveryClient()._build_model_entity(
        provider,
        {
            "id": "custom-model",
            "task": "text-to-image",
            "input_modalities": ["text", "image", "file"],
            "output_modalities": ["image"],
        },
    )

    assert model is not None
    assert model.model_type == ModelType.IMAGE_GENERATION
    assert model.input_modalities == [ModelModality.TEXT, ModelModality.IMAGE]
    assert model.output_modalities == [ModelModality.IMAGE]
    assert model.native_file_input is True


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


def test_manager_uses_only_discovered_models_and_generic_chat_class():
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
    models = manager.get_model_entities(
        "openai",
        discovered_models=[dynamic_model],
    )
    resolved = manager.get_model_entity(
        "openai",
        "vendor-chat",
        discovered_models=[dynamic_model],
    )
    model_class = manager.get_provider("openai").get_model_class(
        resolved.model_type,
        model_entity=resolved,
    )

    assert "vendor-chat" in {model.model_name for model in models}
    assert manager.get_model_entities("openai") == []
    assert resolved.is_dynamic is True
    assert model_class is Chat


def test_language_model_service_loads_discovered_model(monkeypatch):
    manager = LanguageModelManager()
    dynamic_model = ModelEntity(
        model="vendor-chat",
        is_dynamic=True,
        input_modalities=[ModelModality.TEXT, ModelModality.IMAGE],
        attributes={
            "model": "vendor-chat",
            "base_url": "https://vendor.example/v1",
        },
    )
    monkeypatch.setattr(
        "internal.service.language_model_service.get_api_key_for_provider",
        lambda provider_name, account_id: "secret",
    )

    service = LanguageModelService(
        db=None,
        language_model_manager=manager,
    )
    monkeypatch.setattr(
        service,
        "get_enabled_model_entity",
        lambda provider_name, model_name, account_id: dynamic_model,
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
    assert model.input_modalities == [ModelModality.TEXT, ModelModality.IMAGE]


def test_model_catalog_returns_all_enabled_models_by_default(monkeypatch):
    manager = LanguageModelManager()
    service = LanguageModelService(db=None, language_model_manager=manager)
    enabled_models = [
        ModelEntity(model="chat-model", model_type=ModelType.CHAT, is_dynamic=True),
        ModelEntity(
            model="embedding-model",
            model_type=ModelType.EMBEDDING,
            is_dynamic=True,
        ),
        ModelEntity(
            model="image-model",
            model_type=ModelType.IMAGE_GENERATION,
            is_dynamic=True,
        ),
    ]
    monkeypatch.setattr(
        service,
        "get_enabled_model_entities",
        lambda provider_name, account_id: enabled_models,
    )

    catalog = service.get_language_models(uuid4())

    assert catalog
    assert {
        model["model_type"]
        for provider in catalog
        for model in provider["models"]
    } == {"chat", "embedding", "image_generation"}


def test_model_catalog_filters_models_by_type(monkeypatch):
    manager = LanguageModelManager()
    service = LanguageModelService(db=None, language_model_manager=manager)
    enabled_models = [
        ModelEntity(model="chat-model", model_type=ModelType.CHAT, is_dynamic=True),
        ModelEntity(
            model="embedding-model",
            model_type=ModelType.EMBEDDING,
            is_dynamic=True,
        ),
    ]
    monkeypatch.setattr(
        service,
        "get_enabled_model_entities",
        lambda provider_name, account_id: enabled_models,
    )

    catalog = service.get_language_models(
        uuid4(),
        model_types={ModelType.EMBEDDING},
    )

    assert catalog
    returned_models = [
        model
        for provider in catalog
        for model in provider["models"]
    ]
    assert {model["model_name"] for model in returned_models} == {"embedding-model"}
    assert {model["model_type"] for model in returned_models} == {"embedding"}


def test_default_agent_model_skips_specialized_models(monkeypatch):
    manager = LanguageModelManager()
    service = LanguageModelService(db=None, language_model_manager=manager)
    enabled_models = [
        ModelEntity(
            model="embedding-model",
            model_type=ModelType.EMBEDDING,
            is_dynamic=True,
        ),
        ModelEntity(model="chat-model", model_type=ModelType.CHAT, is_dynamic=True),
    ]
    monkeypatch.setattr(
        service,
        "get_enabled_model_entities",
        lambda provider_name, account_id: enabled_models,
    )

    config = service.get_default_model_config(uuid4())

    assert config["model"] == "chat-model"
    assert config["input_modalities"] == ["text"]
    assert config["output_modalities"] == ["text"]
