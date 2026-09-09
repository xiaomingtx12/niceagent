"""Offline regression checks for work performed before the first model request."""

from time import perf_counter
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from internal.exception import ValidateErrorException
from internal.service.language_model_service import LanguageModelService


def model_service(count=200):
    """Use realistic saved snapshots while keeping credentials and database I/O local."""
    profile = SimpleNamespace(
        id=uuid4(), base_url="https://example.invalid/v1", api_format="chat_completions",
        enabled_models=[f"model-{index}" for index in range(count)],
        discovered_models=[{
            "model": f"model-{index}", "context_window": 32768,
            "parameters": [{"name": "reasoning_effort", "options": [
                {"label": level, "value": level} for level in ("low", "medium", "high")
            ]}],
            "metadata": {"parameter_contract_version": 2, "parameters_source": "declared"},
        } for index in range(count)],
    )
    service = object.__new__(LanguageModelService)
    service.language_model_manager = Mock()
    service._get_provider_profiles = Mock(return_value=[profile])
    return service, profile


def test_single_model_lookup_only_normalizes_the_requested_snapshot(monkeypatch):
    """Catalog size must not multiply expensive parameter validation for one model."""
    import internal.service.language_model_service as module

    service, profile = model_service()
    normalize = Mock(wraps=module.normalize_model_parameter_contract)
    monkeypatch.setattr(module, "normalize_model_parameter_contract", normalize)
    started = perf_counter()
    model = service.get_enabled_model_entity("openai", "model-199", uuid4(), str(profile.id))
    elapsed = (perf_counter() - started) * 1000
    print(f"single model from 200 snapshots: {elapsed:.2f} ms; normalized={normalize.call_count}")
    assert model.model_name == "model-199"
    assert normalize.call_count == 1


def test_lookup_keeps_enabled_whitelist_and_does_not_cache_revoked_models():
    """A second request must observe revocation even if the discovery snapshot remains."""
    service, profile = model_service(2)
    account_id = uuid4()
    assert service.get_enabled_model_entity("openai", "model-1", account_id, str(profile.id))
    profile.enabled_models.remove("model-1")
    assert service.get_enabled_model_entity("openai", "model-1", account_id, str(profile.id)) is None
    service._get_provider_profiles.assert_called_with(
        "openai", account_id, provider_api_key_id=str(profile.id),
    )


def test_lookup_preserves_ambiguous_connection_error():
    """An optimization cannot pick an arbitrary connection for duplicate model names."""
    service, first = model_service(1)
    _, second = model_service(1)
    service._get_provider_profiles.return_value = [first, second]
    with pytest.raises(ValidateErrorException, match="多个同名模型连接"):
        service.get_enabled_model_entity("openai", "model-0", uuid4())


def test_lookup_skips_invalid_target_snapshot():
    """Corrupt snapshots retain the existing unavailable-model behavior."""
    service, profile = model_service(1)
    profile.discovered_models[0]["context_window"] = -1
    assert service.get_enabled_model_entity("openai", "model-0", uuid4()) is None
