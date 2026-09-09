"""工作台模型覆盖沿真实 DTO 和装配边界验证，不发起模型调用。"""

from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from internal.core.language_model.entities.model_entity import ModelEntity
from internal.core.language_model.parameters import parse_parameter_definitions
from internal.entity.agent_workbench_entity import WorkbenchModelSelection
from internal.entity.conversation_entity import InvokeFrom
from internal.exception import ValidateErrorException
from internal.schema.agent_runtime_schema import AgentInvocation, AgentInvocationContext
from internal.schema.agent_workbench_schema import WorkbenchCommandInputReq
from internal.schema.app_config_schema import AppModelConfigReq
from internal.service.agent_assembly_service import AgentAssemblyService
from internal.service.workflow_service import WorkflowService


@pytest.fixture
def case():
    """两个连接的模型能力不同，检查旧参数不会泄漏到新模型。"""
    owner, connection = uuid4(), uuid4()
    context = AgentInvocationContext(account_id=owner, app_id=uuid4(), conversation_id=uuid4(), created_by=owner, end_user_id=None, invoke_from=InvokeFrom.CREATOR_TASK)
    original = AppModelConfigReq(provider="openai", model="model-a", provider_connection_id=uuid4(), parameters={"temperature": 0.8, "reasoning_effort": "high"})
    selection = WorkbenchModelSelection(provider="openai", model="model-b", provider_connection_id=connection)
    model = ModelEntity(model="model-b", parameters=parse_parameter_definitions([
        {"name": "reasoning_effort", "options": [{"label": v, "value": v} for v in ["low", "medium"]], "default": "medium"},
    ]), attributes={"provider_api_key_id": str(connection)})
    service = object.__new__(AgentAssemblyService)
    service.language_model_service = Mock()
    service.language_model_service.get_enabled_model_entity.return_value = model
    service.language_model_service.get_language_models.return_value = [{
        "provider_name": "openai", "connection_id": str(connection), "connection_name": "Local",
        "models": [model.model_dump(mode="json")],
    }]
    service._load_app_configuration = Mock(return_value=("Agent", "", owner, SimpleNamespace(model_config_data=original)))
    return SimpleNamespace(service=service, context=context, selection=selection, original=original, model=model)


def test_selection_survives_command_invocation_and_resume(case):
    """协议解析和持久化恢复都必须保留实际选择的连接。"""
    command = WorkbenchCommandInputReq(query="hello", model_selection=case.selection, reasoning_effort="low")
    run = command.to_run_input()
    invocation = AgentInvocation(**case.context.model_dump(), message_id=uuid4(), **run.model_dump(exclude={"file_ids"}))
    restored = AgentInvocation.model_validate_json(invocation.model_dump_json())
    assert restored.model_selection == case.selection
    assert restored.reasoning_effort == "low"


@pytest.mark.parametrize("extra", [{"api_key": "not-allowed"}, {"base_url": "https://example.invalid"}, {"parameters": {}}])
def test_selection_rejects_arbitrary_runtime_settings(case, extra):
    """客户端只能引用白名单模型，不能绕过服务连接设置。"""
    with pytest.raises(ValidationError):
        WorkbenchModelSelection(**{**case.selection.model_dump(), **extra})


def test_new_model_drops_old_parameters_and_preserves_agent_config(case):
    """新模型使用自身参数；Agent 编排参数不受本轮覆盖影响。"""
    config = case.service.resolve_workbench_model(case.context, case.selection)
    assert config.model == "model-b"
    assert config.parameters == {}
    assert case.original.parameters == {"temperature": 0.8, "reasoning_effort": "high"}
    case.service.language_model_service.get_enabled_model_entity.assert_called_with("openai", "model-b", case.context.account_id, provider_connection_id=str(case.selection.provider_connection_id))
    same = WorkbenchModelSelection(provider=case.original.provider, model=case.original.model, provider_connection_id=case.original.provider_connection_id)
    assert case.service.resolve_workbench_model(case.context, same) == case.original


@pytest.mark.parametrize("source", [InvokeFrom.WEB_APP, InvokeFrom.SERVICE_API])
def test_public_entries_cannot_select_other_models(case, source):
    """发布链接访问者不能通过自造协议读取或使用账号模型目录。"""
    context = case.context.model_copy(update={"invoke_from": source})
    with pytest.raises(ValidateErrorException):
        case.service.resolve_workbench_model(context, case.selection)
    assert case.service._workbench_model_choices(context) == []


def test_rejects_foreign_owner_disabled_model_and_unsupported_effort(case):
    """切换和强度在创建消息前校验，以免无效调用进入执行队列。"""
    with pytest.raises(ValidateErrorException):
        case.service.resolve_workbench_model(case.context.model_copy(update={"created_by": uuid4()}), case.selection)
    case.service.validate_workbench_reasoning(case.context, "low", case.selection)
    with pytest.raises(ValidateErrorException):
        case.service.validate_workbench_reasoning(case.context, "high", case.selection)
    case.service.language_model_service.get_enabled_model_entity.return_value = None
    with pytest.raises(ValidateErrorException):
        case.service.validate_workbench_reasoning(case.context, None, case.selection)


def test_catalog_only_contains_declared_levels(case):
    """枚举为空时不能补出通用思考等级。"""
    settings = case.service.get_workbench_model_settings(case.context, case.selection)
    assert settings.selected_model == case.selection
    assert settings.reasoning_levels == ["low", "medium"]
    assert settings.available_models[0].reasoning_levels == ["low", "medium"]
    case.model.parameters[0].options = []
    assert case.service.get_workbench_model_settings(case.context, case.selection).reasoning_levels == []


def test_submission_only_resolves_the_selected_model_once(case):
    """Submitting a message must not build the model-picker catalog or resolve twice."""
    case.service.validate_workbench_reasoning(case.context, "low", case.selection)
    case.service.language_model_service.get_language_models.assert_not_called()
    case.service.language_model_service.get_enabled_model_entity.assert_called_once()
    case.service._load_app_configuration.assert_called_once()


def test_actual_assembly_loads_selected_model(case):
    """检查真实装配方法把覆盖传到模型加载器，而不仅仅保存在 DTO。"""
    service = case.service
    config = SimpleNamespace(model_config_data=case.original, subagents=[])
    service._load_app_configuration.return_value = ("Agent", "", case.context.account_id, config)
    service._validate_selected_skills = Mock(return_value=[])
    service._log_build_timing = Mock()
    service._build_configured_tools = Mock(return_value=[])
    service._prepare_subagents = Mock(return_value=([], {}))
    invocation = AgentInvocation(**case.context.model_dump(), message_id=uuid4(), query="hello", model_selection=case.selection, reasoning_effort="low")
    result = service._resolve_definition(invocation, Mock())
    loaded = service.language_model_service.load_language_model.call_args.args[0]
    assert loaded.model == "model-b"
    assert loaded.parameters == {"reasoning_effort": "low"}
    assert result.model is service.language_model_service.load_language_model.return_value


def test_workflow_new_selection_wins_over_legacy_alias(case):
    """节点编辑后的规范字段不能被旧快照字段覆盖。"""
    service = object.__new__(WorkflowService)
    service.language_model_service = case.service.language_model_service
    node = {"node_type": "llm", "model_config": {**case.selection.model_dump(mode="json"), "parameters": {"reasoning_effort": "low"}}, "language_model_config": case.original.model_dump(mode="json")}
    service._validate_model_parameters({"nodes": [node]}, SimpleNamespace(id=case.context.account_id))
    assert "language_model_config" not in node
    assert node["model_config"]["model"] == "model-b"
    assert node["model_config"]["parameters"] == {"reasoning_effort": "low"}
