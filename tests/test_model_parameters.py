"""模型发现参数契约测试：不存在旧通用默认值，运行和保存使用同一校验。"""

import pytest
from importlib import import_module
import json
import httpx
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from internal.core.language_model.entities.model_entity import DEFAULT_CONTEXT_WINDOW, ModelEntity
from internal.core.language_model.parameters import (
    canonicalize_parameters,
    parse_parameter_definitions,
    runtime_model_parameters,
    validate_model_parameters,
    normalize_model_parameter_contract,
)


def discovered_model(parameters, *, output=4096):
    """构造只包含模型发现能力的动态模型。"""
    return ModelEntity(
        model="discovered", is_dynamic=True, context_window=8192,
        max_output_tokens=output, parameters=parse_parameter_definitions(parameters, output_limit=output),
        metadata={"parameter_contract_version": 2, "parameters_source": "declared"},
    )


def test_unknown_model_has_no_legacy_parameter_template():
    """目录没有声明参数时，旧 temperature/top_p/max_tokens 不再自动出现。"""
    model = discovered_model(None)
    assert model.parameters == []
    assert validate_model_parameters(model, {}) == {}


def test_declared_parameters_are_strict_and_optional_values_are_omitted():
    """可选参数未设置时不发送服务默认值，设置 0 仍然有效。"""
    model = discovered_model([
        {"name": "temperature", "type": "float", "min": 0, "max": 2, "default": 1},
        {"name": "max_tokens", "type": "int", "api_name": "max_completion_tokens", "min": 1, "max": 4096},
    ])
    assert validate_model_parameters(model, {}) == {}
    assert validate_model_parameters(model, {"temperature": 0})["temperature"] == 0
    with pytest.raises(ValueError, match="不能小于"):
        validate_model_parameters(model, {"max_output_tokens": 0})
    assert runtime_model_parameters(model, {"temperature": 0, "max_output_tokens": 400}) == {
        "temperature": 0, "extra_body": {"max_completion_tokens": 400},
    }


def test_declared_top_k_is_forwarded_as_provider_specific_sampling_parameter():
    """Top K is distinct from retrieval K and is sent only when declared."""
    model = discovered_model([{"name": "top_k", "type": "int", "min": 1, "max": 100}])
    assert runtime_model_parameters(model, {"top_k": 1}) == {"extra_body": {"top_k": 1}}
    with pytest.raises(ValueError, match="不能大于"):
        runtime_model_parameters(model, {"top_k": 101})


@pytest.mark.parametrize("value", [True, 1.5, "40", 0, 101])
def test_top_k_save_and_runtime_share_strict_validation(value):
    """Reject type and range errors before a saved configuration reaches the SDK."""
    model = discovered_model([{"name": "top_k", "minimum": 1, "maximum": 100}])
    for validate in (validate_model_parameters, runtime_model_parameters):
        with pytest.raises(ValueError):
            validate(model, {"top_k": value})


def test_top_k_support_and_sentinel_values_are_provider_declared():
    """Do not invent model support, ranges, or special disable values."""
    with pytest.raises(ValueError, match="未声明支持"):
        runtime_model_parameters(discovered_model([]), {"top_k": 40})
    model = discovered_model([{"name": "top_k", "min": -1, "max": 100, "default": 40}])
    assert runtime_model_parameters(model, {}) == {}
    assert runtime_model_parameters(model, {"top_k": -1}) == {"extra_body": {"top_k": -1}}


def test_reasoning_support_does_not_invent_levels():
    """A name-only declaration permits explicit strings without fabricating an enum."""
    model = discovered_model(["reasoning_effort"])
    assert model.parameters[0].options == []
    assert runtime_model_parameters(model, {}) == {}
    assert runtime_model_parameters(model, {"reasoning_effort": "provider-custom"}) == {
        "reasoning_effort": "provider-custom",
    }
    with pytest.raises(ValueError):
        runtime_model_parameters(model, {"thinking": {"type": "enabled"}})


@pytest.mark.parametrize("provider", ["openai", "deepseek", "doubao", "minimax", "moonshot", "tongyi", "wenxin", "zhipu"])
@pytest.mark.parametrize("level", [None, "medium"])
def test_top_k_snapshot_reaches_real_sdk_json_with_other_overrides(provider, level):
    """All compatible adapters retain validated fields across snapshot and SDK boundaries."""
    from internal.service.provider_api_key_service import ProviderApiKeyService

    chat = import_module(f"internal.core.language_model.providers.{provider}.chat").Chat
    model = discovered_model([
        {"name": "top_k", "min": 1, "max": 100},
        {"name": "thinking_level", "enum": ["low", "medium", "high"]},
        "max_tokens",
    ])
    snapshot = ProviderApiKeyService._normalize_discovered_models([model.model_dump(by_alias=True)])[0]
    restored = ModelEntity.model_validate(snapshot)
    payloads = []

    def handle(request):
        """Capture local HTTP payloads without accessing a provider or credentials."""
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
            "model": "discovered", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        llm = chat(model="discovered", api_key="local-test", base_url="https://example.invalid/v1",
                   http_client=client, **runtime_model_parameters(restored, {
                       "top_k": 40, "reasoning_effort": level, "max_output_tokens": 512,
                   }))
        assert llm.invoke("test").content == "ok"
    payload = payloads[0]
    assert payload["top_k"] == 40
    assert payload["max_tokens"] == 512
    assert payload.get("thinking_level") == level
    if level is None:
        assert "thinking_level" not in payload
    assert not {"extra_body", "model_kwargs", "reasoning_effort", "temperature", "top_p"} & payload.keys()


def test_legacy_output_aliases_are_normalized_and_conflicts_are_rejected():
    """旧配置可读取一次；两个旧字段同时存在且不同必须让用户修复。"""
    assert canonicalize_parameters({"max_tokens": 300})["max_output_tokens"] == 300
    with pytest.raises(ValueError, match="冲突"):
        canonicalize_parameters({"max_tokens": 300, "max_completion_tokens": 400})


def test_discovered_output_limit_caps_declared_parameter():
    """发现结果的最大输出能力优先于手工参数上限。"""
    model = discovered_model([{"name": "max_output_tokens", "api_name": "max_tokens", "type": "int", "max": 10000}], output=2048)
    with pytest.raises(ValueError, match="不能大于"):
        validate_model_parameters(model, {"max_output_tokens": 2049})


def test_invalid_discovered_definition_is_rejected():
    """模型发现的坏参数不能进入启用快照，避免运行时才失败。"""
    with pytest.raises(ValueError, match="API 字段"):
        parse_parameter_definitions([{"name": "max_output_tokens", "api_name": "temperature", "type": "int"}])


def test_old_snapshot_template_is_not_trusted_as_a_capability():
    """旧快照仍可启用模型，但无声明版本的通用参数全部停用，不改原始数据库字典。"""
    model = discovered_model(["temperature", "top_p", "max_tokens"])
    model.metadata = {"source": "discovered"}
    normalize_model_parameter_contract(model)
    assert model.parameters == []
    assert model.metadata["parameters_source"] == "unknown"
    with pytest.raises(ValueError, match="未声明支持"):
        validate_model_parameters(model, {"temperature": 1})


def test_discovery_uses_declared_limits_and_parameters_only():
    """输入上限不等于完整窗口，名称列表只启用声明字段。"""
    from internal.core.language_model.model_discovery import ModelDiscoveryClient
    from internal.core.language_model.entities.provider_entity import ProviderEntity
    discovery = ModelDiscoveryClient()
    provider = ProviderEntity(name="openai", base_url="https://example.invalid/v1")
    unknown = discovery._build_model_entity(provider, {"id": "chat-new"})
    assert unknown.parameters == [] and unknown.context_window == DEFAULT_CONTEXT_WINDOW
    assert unknown.metadata["context_window_source"] == "default"
    model = discovery._build_model_entity(provider, {"id": "chat-new", "input_token_limit": 32000,
        "output_token_limit": 1000, "supported_parameters": ["max_completion_tokens", "reasoning_effort"]})
    assert model.context_window == DEFAULT_CONTEXT_WINDOW and model.max_input_tokens == 32000
    assert [p.name for p in model.parameters] == ["max_output_tokens", "reasoning_effort"]
    assert model.parameters[0].max == 1000


@pytest.mark.parametrize("api_name", ["max_tokens", "max_completion_tokens", "max_output_tokens"])
def test_sdk_request_uses_exact_declared_output_field(api_name):
    """捕获 SDK 真正 HTTP JSON，避免只测试构造参数却漏掉 SDK 重命名。"""
    from internal.core.language_model.providers.openai.chat import Chat
    requests = []

    def handle(request):
        """使用本地传输替代网络，返回标准 Chat Completions 响应。"""
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
            "model": "discovered", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]})

    model = discovered_model([{"name": api_name}])
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        llm = Chat(model="discovered", api_key="local-test", base_url="https://example.invalid/v1", http_client=client,
                   **runtime_model_parameters(model, {"max_output_tokens": 512}))
        llm.invoke("test")
    assert requests[0][api_name] == 512
    assert sum(name in requests[0] for name in ("max_tokens", "max_completion_tokens", "max_output_tokens")) == 1
    assert "temperature" not in requests[0] and "top_p" not in requests[0]


def test_saved_config_validation_and_runtime_reject_same_invalid_values():
    """不同入口使用同一规则，枚举比较的是值而不是 option 对象。"""
    model = discovered_model([{"name": "reasoning_effort", "enum": ["low", "medium"]}, {"name": "temperature", "minimum": 0, "maximum": 1}])
    assert validate_model_parameters(model, {"reasoning_effort": "medium", "temperature": 0}) == {"reasoning_effort": "medium", "temperature": 0}
    for values in [{"reasoning_effort": "high"}, {"temperature": True}, {"temperature": float('nan')}, {"api_key": "secret"}, {"extra_body": {"x": 1}}]:
        with pytest.raises(ValueError):
            runtime_model_parameters(model, values)


def test_connection_snapshot_cannot_override_generation_settings_through_attributes():
    """旧固定 attributes 不能重新注入已清理的生成参数。"""
    from internal.service.language_model_service import LanguageModelService
    service = LanguageModelService(db=Mock(), language_model_manager=Mock())
    model = discovered_model([])
    connection_id = uuid4()
    model.attributes = {"model": "discovered", "base_url": "https://example.invalid/v1", "provider_api_key_id": str(connection_id), "temperature": 9}
    service.get_enabled_model_entity = Mock(return_value=model)
    service.db.session.query.return_value.filter.return_value.first.return_value = SimpleNamespace(api_key="test")
    constructor = Mock()
    service.language_model_manager.get_provider.return_value.get_model_class.return_value = constructor
    service.load_language_model({"provider": "openai", "model": "discovered", "provider_connection_id": str(connection_id), "parameters": {}}, account_id=uuid4())
    assert "temperature" not in constructor.call_args.kwargs
    assert constructor.call_args.kwargs["profile"] == {"max_input_tokens": 8192, "max_output_tokens": 4096}


@pytest.mark.parametrize("api_name", ["reasoning_effort", "thinking_level", "thinking"])
@pytest.mark.parametrize("level", [None, "low", "medium", "high", "xhigh"])
def test_thinking_level_survives_snapshot_and_sdk_request(api_name, level):
    """Exercise saved capabilities and actual SDK JSON, including service defaults."""
    from internal.service.provider_api_key_service import ProviderApiKeyService
    from internal.core.language_model.providers.openai.chat import Chat

    model = discovered_model([
        {"name": api_name, "enum": ["low", "medium", "high", "xhigh"]},
        {"name": "max_tokens"},
    ])
    snapshots = ProviderApiKeyService._normalize_discovered_models([model.model_dump(by_alias=True)])
    restored = ModelEntity.model_validate(snapshots[0])
    parameters = {"max_output_tokens": 512, "reasoning_effort": level}
    requests = []

    def handle(request):
        """Capture the SDK payload without sending any external request."""
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
            "model": "discovered", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        llm = Chat(model="discovered", api_key="local-test", base_url="https://example.invalid/v1",
                   http_client=client, **runtime_model_parameters(restored, parameters))
        assert llm.invoke("test").content == "ok"
    payload = requests[0]
    assert payload["max_tokens"] == 512
    assert {key: payload[key] for key in ("reasoning_effort", "thinking_level", "thinking") if key in payload} == (
        {api_name: level} if level is not None else {}
    )


def test_default_context_survives_legacy_snapshot_and_manual_override():
    """Missing legacy windows gain the default; explicit limits and sources survive reload."""
    from internal.service.provider_api_key_service import ProviderApiKeyService

    snapshots = ProviderApiKeyService._normalize_discovered_models([
        {"model": "old", "context_window": 0},
        {"model": "manual", "context_window": 65536, "metadata": {"context_window_source": "manual"}},
        {"model": "directory", "context_window": 8192},
    ])
    restored = [ModelEntity.model_validate(snapshot) for snapshot in snapshots]
    assert [model.context_window for model in restored] == [DEFAULT_CONTEXT_WINDOW, 65536, 8192]
    assert [model.metadata["context_window_source"] for model in restored] == ["default", "manual", "declared"]
    assert ModelEntity(model="embed", model_type="embedding").context_window == 0
