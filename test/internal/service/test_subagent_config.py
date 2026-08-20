"""Creator app configuration contract for DeepAgents teams."""

from types import SimpleNamespace
from uuid import UUID

import pytest

from internal.core.agent_v2.runtime.subagents import (
    SubAgentSpecError,
    validate_platform_subagents,
)
from internal.entity.app_entity import AppStatus, DEFAULT_APP_CONFIG
from internal.exception import ValidateErrorException
from internal.service.app_config_service import AppConfigService


class _Query:
    def __init__(self, target):
        self.target = target

    def filter(self, *args):
        return self

    def one_or_none(self):
        return self.target


class _Session:
    def __init__(self, targets):
        self.targets = iter(targets)

    def query(self, _model):
        return _Query(next(self.targets))


def _service_for_targets(*targets):
    service = object.__new__(AppConfigService)
    service.db = SimpleNamespace(session=_Session(targets))
    return service


def _published_target(app_id, *, subagents=None):
    return SimpleNamespace(
        id=app_id,
        account_id=UUID("00000000-0000-0000-0000-000000000001"),
        status=AppStatus.PUBLISHED,
        app_config=SimpleNamespace(subagents=subagents or []),
    )
def test_default_app_config_starts_with_empty_team():
    assert DEFAULT_APP_CONFIG["subagents"] == []


def test_draft_validation_normalizes_subagents_without_other_dependencies():
    service = object.__new__(AppConfigService)
    result = service.validate_draft_app_config(
        {
            "subagents": {
                "researcher": {
                    "description": "  负责搜索资料  ",
                    "system_prompt": "  仅返回有来源的研究结论  ",
                }
            }
        },
        account_id=None,
    )
    assert result["subagents"] == [
        {
            "name": "researcher",
            "description": "负责搜索资料",
            "system_prompt": "仅返回有来源的研究结论",
            "mode": "sync",
            "context_passing": "NONE",
            "tools": [],
            "skills": [],
        }
    ]


def test_draft_validation_maps_subagent_error_to_api_validation_error():
    service = object.__new__(AppConfigService)
    with pytest.raises(ValidateErrorException) as exc_info:
        service.validate_draft_app_config(
            {
                "subagents": {
                    "Invalid Name": {
                        "description": "研究",
                        "system_prompt": "做研究",
                    }
                }
            },
            account_id=None,
        )
    assert "名称" in exc_info.value.message


def test_draft_validation_accepts_async_subagent_contract():
    service = object.__new__(AppConfigService)
    result = service.validate_draft_app_config(
        {
            "subagents": [
                {
                    "name": "remote_researcher",
                    "description": "远程研究",
                    "mode": "async",
                    "graph_id": "research-graph",
                    "url": "https://agent.example.com",
                    "headers": {},
                    "tools": [],
                    "skills": [],
                }
            ]
        },
        account_id=None,
    )

    assert result["subagents"][0]["mode"] == "async"
    assert result["subagents"][0]["url"] == "https://agent.example.com"


def test_published_subagent_reference_requires_same_account_published_target():
    target_id = UUID("00000000-0000-0000-0000-000000000010")
    service = _service_for_targets(None)
    config = validate_platform_subagents(
        [
            {
                "name": "published_researcher",
                "description": "调用已发布研究 Agent",
                "source": "published",
                "published_app_id": str(target_id),
                "tools": [],
                "skills": [],
            }
        ]
    )

    with pytest.raises(SubAgentSpecError, match="不存在、未发布或当前账号不可见"):
        service.validate_published_agent_references(
            config,
            account_id=UUID("00000000-0000-0000-0000-000000000001"),
        )


def test_published_subagent_reference_rejects_self_and_cycles():
    root_id = UUID("00000000-0000-0000-0000-000000000020")
    nested_id = UUID("00000000-0000-0000-0000-000000000021")
    published_ref = lambda target_id: {
        "name": f"published_{str(target_id)[-4:]}",
        "description": "nested",
        "source": "published",
        "published_app_id": str(target_id),
        "tools": [],
        "skills": [],
    }

    service = _service_for_targets(_published_target(root_id))
    with pytest.raises(SubAgentSpecError, match="不能引用当前应用自身"):
        service.validate_published_agent_references(
            [published_ref(root_id)],
            account_id=UUID("00000000-0000-0000-0000-000000000001"),
            app_id=root_id,
        )

    cycle_root = _published_target(nested_id, subagents=[published_ref(root_id)])
    cycle_nested = _published_target(root_id, subagents=[published_ref(nested_id)])
    service = _service_for_targets(cycle_root, cycle_nested)
    with pytest.raises(SubAgentSpecError, match="存在循环"):
        service.validate_published_agent_references(
            [published_ref(nested_id)],
            account_id=UUID("00000000-0000-0000-0000-000000000001"),
            app_id=UUID("00000000-0000-0000-0000-000000000099"),
        )


def test_app_config_response_includes_persisted_team():
    record = SimpleNamespace(
        id="config-id",
        dialog_round=3,
        preset_prompt="main",
        subagents=[
            {
                "name": "researcher",
                "description": "负责研究",
                "system_prompt": "你是研究员",
                "mode": "sync",
                "context_passing": "NONE",
                "tools": [],
                "skills": [],
            }
        ],
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
