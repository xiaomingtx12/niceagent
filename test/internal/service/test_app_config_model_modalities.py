from types import SimpleNamespace

from internal.core.language_model.entities.model_entity import (
    ModelEntity,
    ModelModality,
)
from internal.service.app_config_service import AppConfigService
from internal.service.web_app_service import WebAppService


def _app_config_service(model: ModelEntity) -> AppConfigService:
    service = object.__new__(AppConfigService)
    service.language_model_manager = SimpleNamespace(
        get_provider=lambda _provider_name: object()
    )
    service.language_model_service = SimpleNamespace(
        get_enabled_model_entity=lambda *_args: model
    )
    return service


def test_strict_model_config_derives_capabilities_from_enabled_snapshot():
    service = _app_config_service(
        ModelEntity(
            model="relay-vision-model",
            is_dynamic=True,
            input_modalities=[ModelModality.TEXT, ModelModality.IMAGE],
        )
    )

    result = service._validate_strict_model_config(
        {
            "provider": "relay",
            "model": "relay-vision-model",
            "parameters": {},
            "input_modalities": ["text"],
        }
    )

    assert result["input_modalities"] == ["text", "image"]
    assert result["output_modalities"] == ["text"]
    assert result["native_file_input"] is False


def test_web_app_info_exposes_model_input_capabilities():
    service = object.__new__(WebAppService)
    app = SimpleNamespace(
        id="app-id",
        icon="icon",
        name="App",
        description="Description",
    )
    service.get_web_app = lambda _token: app
    service.app_config_service = SimpleNamespace(
        get_app_config=lambda _app: {
            "model_config": {
                "input_modalities": ["text", "image"],
                "native_file_input": True,
            },
            "opening_statement": "Hello",
            "opening_questions": [],
            "suggested_after_answer": {"enable": True},
            "text_to_speech": {"enable": False},
            "speech_to_text": {"enable": False},
        }
    )

    result = service.get_web_app_info("token")

    assert result["app_config"]["input_modalities"] == ["text", "image"]
    assert result["app_config"]["native_file_input"] is True
    assert "features" not in result["app_config"]
