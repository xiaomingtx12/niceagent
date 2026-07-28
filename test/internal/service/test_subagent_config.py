"""Creator app configuration contract for DeepAgents teams."""

from types import SimpleNamespace

import pytest

from internal.entity.app_entity import DEFAULT_APP_CONFIG
from internal.exception import ValidateErrorException
from internal.service.app_config_service import AppConfigService
from internal.service.app_service import AppService


def test_default_app_config_starts_with_empty_team():
    assert DEFAULT_APP_CONFIG["subagents"] == {}


def test_draft_validation_normalizes_subagents_without_other_dependencies():
    service = object.__new__(AppService)
    result = service._validate_draft_app_config(
        {
            "subagents": {
                "researcher": {
                    "description": "  负责搜索资料  ",
                    "system_prompt": "  仅返回有来源的研究结论  ",
                }
            }
        },
        account=None,
    )
    assert result["subagents"]["researcher"] == {
        "description": "负责搜索资料",
        "system_prompt": "仅返回有来源的研究结论",
    }


def test_draft_validation_maps_subagent_error_to_api_validation_error():
    service = object.__new__(AppService)
    with pytest.raises(ValidateErrorException) as exc_info:
        service._validate_draft_app_config(
            {
                "subagents": {
                    "Invalid Name": {
                        "description": "研究",
                        "system_prompt": "做研究",
                    }
                }
            },
            account=None,
        )
    assert "名称" in exc_info.value.message


def test_app_config_response_includes_persisted_team():
    record = SimpleNamespace(
        id="config-id",
        dialog_round=3,
        preset_prompt="main",
        subagents={
            "researcher": {
                "description": "负责研究",
                "system_prompt": "你是研究员",
            }
        },
        retrieval_config={},
        long_term_memory={},
        opening_statement="",
        opening_questions=[],
        speech_to_text={},
        text_to_speech={},
        suggested_after_answer={},
        review_config={},
        updated_at=None,
        created_at=None,
    )
    result = AppConfigService._process_and_transformer_app_config(
        model_config={},
        tools=[],
        workflows=[],
        datasets=[],
        app_config=record,
    )
    assert result["subagents"] == record.subagents
