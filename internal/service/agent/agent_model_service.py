"""Agent 模型选择规则：默认、手动覆盖、连接授权与推理参数共用一个解析入口。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID
from injector import inject

from internal.core.language_model.entities.model_entity import ModelEntity, ModelParameter, ModelType
from internal.core.language_model.parameters import canonicalize_parameters
from internal.entity.agent_workbench_entity import WorkbenchModelSelection
from internal.entity.conversation_entity import InvokeFrom
from internal.exception import NotFoundException, ValidateErrorException
from internal.schema.agent.agent_model_schema import (
    WorkbenchModelSettingsResp,
    WorkbenchModelChoiceResp,
)
from internal.schema.app.app_config_schema import AppModelConfigReq
from internal.service.language_model.language_model_service import LanguageModelService
from .agent_definition_service import AgentDefinitionService

_MODEL_SELECTABLE_SOURCES = frozenset({
    InvokeFrom.DEBUGGER, InvokeFrom.CREATOR_TASK, InvokeFrom.ASSISTANT_AGENT,
})


@inject
@dataclass
class AgentModelService:
    """按应用定义解析模型选择；模型目录和连接凭证由 LanguageModelService 管理。"""

    language_model_service: LanguageModelService
    definition_service: AgentDefinitionService

    def restore_workbench_model_settings(
        self, *, account_id: UUID, app_id: UUID, created_by: UUID, invoke_from: InvokeFrom,
        saved: Mapping[str, Any],
    ) -> WorkbenchModelSettingsResp:
        """一次读取默认配置，恢复有效历史覆盖；覆盖失效时就地回退，不重复查询应用。"""
        # 1.默认来源只读取一次；旧客户端的默认展示值不能变成永久模型绑定。
        if saved.get("model_selection") and saved.get("model_selection_explicit") is not True:
            saved = {}
        try:
            default_config, owner_account_id = self._load_default_configuration(
                account_id=account_id, app_id=app_id,
                invoke_from=invoke_from,
            )
        except (NotFoundException, ValidateErrorException, ValueError):
            return WorkbenchModelSettingsResp()
        try:
            raw_selection = saved.get("model_selection")
            selection = WorkbenchModelSelection.model_validate(raw_selection) if raw_selection else None
        except ValueError:
            selection, saved = None, {}

        # 2.先尝试历史覆盖，失效后只用已读取的默认配置回退一次。
        selections = (selection, None) if selection is not None else (None,)
        for selection in selections:
            try:
                config, model = self.resolve(
                    account_id=account_id, created_by=created_by,
                    invoke_from=invoke_from, default_config=default_config,
                    selection=selection, owner_account_id=owner_account_id,
                )
                if config is not None and model is None:
                    model = self._get_model_entity(config, owner_account_id)
                if config is None or model is None:
                    return WorkbenchModelSettingsResp()

                # 3.思考强度优先采用应用配置，未设置时才采用模型声明的默认值。
                parameter = self._reasoning_parameter(model)
                parameters = canonicalize_parameters(config.parameters)
                default = parameters.get("reasoning_effort")
                reasoning_levels = []
                if parameter is not None:
                    reasoning_levels = [option.value for option in parameter.options]
                    if default is None:
                        default = parameter.default

                # 4.没有显式选择时，仅为允许切换模型的创建者补全当前连接。
                selected_model = selection
                can_select_model = invoke_from in _MODEL_SELECTABLE_SOURCES and created_by == owner_account_id
                connection_id = config.provider_connection_id or model.attributes.get("provider_api_key_id")
                needs_default_selection = selected_model is None and can_select_model
                if needs_default_selection and connection_id:
                    selected_model = WorkbenchModelSelection(
                        provider=config.provider,
                        model=config.model,
                        provider_connection_id=connection_id,
                    )

                # 5.构造响应时只引用已算好的字段，不在响应内部混入权限和回退分支。
                return WorkbenchModelSettingsResp(
                    model=config.model,
                    provider=config.provider,
                    selected_model=selected_model,
                    model_override=selection,
                    available_models=self._workbench_model_choices(
                        account_id=account_id, created_by=created_by,
                        invoke_from=invoke_from,
                    ),
                    reasoning_levels=reasoning_levels,
                    default_reasoning_effort=default,
                    selected_reasoning_effort=(
                        saved.get("reasoning_effort") if saved.get("reasoning_effort") in reasoning_levels else None
                    ),
                )
            except (NotFoundException, ValidateErrorException, ValueError):
                # 无效覆盖的思考强度不能被带到回退模型。
                saved = {}
        return WorkbenchModelSettingsResp()

    def validate_workbench_reasoning(
        self, *, account_id: UUID, app_id: UUID, created_by: UUID, invoke_from: InvokeFrom,
        effort: str | None, selection: WorkbenchModelSelection | None = None,
    ) -> None:
        """任务覆盖只允许当前模型明确声明的等级，在创建消息前拒绝无效选择。"""
        # 1.完全没有覆盖时不查模型；只切换模型时仍需校验连接权限。
        if selection is None and effort is None:
            return
        default_config, owner_account_id = self._load_default_configuration(
            account_id=account_id, app_id=app_id,
            invoke_from=invoke_from,
        )
        config, model = self.resolve(
            account_id=account_id, created_by=created_by,
            invoke_from=invoke_from, default_config=default_config,
            selection=selection, owner_account_id=owner_account_id,
        )
        if effort is None:
            return
        if model is None and config is not None:
            model = self._get_model_entity(config, account_id)

        # 2.必须在该模型声明的可选强度内，不能仅凭参数名称接受客户端输入。
        parameter = self._reasoning_parameter(model)
        if parameter is None:
            raise ValidateErrorException("当前模型不支持所选思考强度，请重新选择")
        allowed_efforts = {option.value for option in parameter.options}
        if effort not in allowed_efforts:
            raise ValidateErrorException("当前模型不支持所选思考强度，请重新选择")

    def resolve(
        self, *, account_id: UUID, created_by: UUID, invoke_from: InvokeFrom,
        default_config: AppModelConfigReq | None, selection: WorkbenchModelSelection | None,
        owner_account_id: UUID,
    ) -> tuple[AppModelConfigReq | None, ModelEntity | None]:
        """保留应用默认参数；只有切换到其他模型或连接时才采用新模型默认值。"""
        # 1.公开入口不能借用账号的其他模型连接。
        if selection is not None and invoke_from not in _MODEL_SELECTABLE_SOURCES:
            raise ValidateErrorException("当前入口不允许切换模型")

        config = default_config
        if selection is None:
            return config, None

        # 2.模型覆盖必须同时满足应用归属和调用者身份，且连接仍然启用。
        if owner_account_id != account_id or created_by != owner_account_id:
            raise ValidateErrorException("只能选择当前账号的模型")
        model = self.language_model_service.get_enabled_model_entity(
            selection.provider, selection.model, owner_account_id,
            provider_connection_id=str(selection.provider_connection_id),
        )
        if model is None or model.model_type != ModelType.CHAT:
            raise ValidateErrorException("所选模型未激活或不是对话模型")

        # 3.旧配置未记录连接 ID 时沿用原参数；明确换连接则重建默认参数。
        same_model = config is not None and (
            config.provider == selection.provider and config.model == selection.model
        )
        same_connection = config is not None and (
            config.provider_connection_id is None
            or config.provider_connection_id == selection.provider_connection_id
        )
        if same_model and same_connection:
            return config.model_copy(update={"provider_connection_id": selection.provider_connection_id}), model
        parameters = {
            parameter.name: parameter.default for parameter in model.parameters
            if parameter.required and parameter.default is not None
        }
        selected_config = AppModelConfigReq(**selection.model_dump(), parameters=parameters)
        return selected_config, model

    def _load_default_configuration(
        self, *, account_id: UUID, app_id: UUID, invoke_from: InvokeFrom,
    ) -> tuple[AppModelConfigReq | None, UUID]:
        """模型服务决定默认模型来源；应用定义服务只负责草稿与发布配置读取。"""
        if invoke_from is InvokeFrom.ASSISTANT_AGENT:
            config = self.language_model_service.get_default_model_config(account_id)
            return AppModelConfigReq.model_validate(config), account_id
        definition = self.definition_service.load(
            account_id=account_id, app_id=app_id, invoke_from=invoke_from,
        )
        return definition.config.model_config_data, definition.account_id

    def _workbench_model_choices(
        self, *, account_id: UUID, created_by: UUID, invoke_from: InvokeFrom,
    ) -> list[WorkbenchModelChoiceResp]:
        """目录按可信账号和创建者身份过滤，不向 WebApp 或开放 API 暴露。"""
        if invoke_from not in _MODEL_SELECTABLE_SOURCES:
            return []
        if created_by != account_id:
            return []

        # 1.按账号一次读取模型目录，每个连接保留自己的模型与参数选项。
        choices = []
        groups = self.language_model_service.get_language_models(account_id, {ModelType.CHAT})
        for group in groups:
            for model in group["models"]:
                parameter = None
                for item in model.get("parameters", []):
                    if item["name"] == "reasoning_effort":
                        parameter = item
                        break
                reasoning_levels = []
                default_effort = None
                if parameter:
                    reasoning_levels = [option["value"] for option in parameter.get("options", [])]
                    default_effort = parameter.get("default")
                choices.append(WorkbenchModelChoiceResp(
                    provider=group["provider_name"],
                    model=model["model_name"],
                    provider_connection_id=group["connection_id"],
                    label=model.get("label") or model["model_name"],
                    connection_name=group["connection_name"],
                    reasoning_levels=reasoning_levels,
                    default_reasoning_effort=default_effort,
                ))
        return choices

    def _get_model_entity(self, config: AppModelConfigReq, account_id: UUID) -> ModelEntity | None:
        """查询展示和参数校验共用的模型定义，连接权限仍由模型服务校验。"""
        connection_id = str(config.provider_connection_id) if config.provider_connection_id else None
        return self.language_model_service.get_enabled_model_entity(
            config.provider, config.model, account_id,
            provider_connection_id=connection_id,
        )

    @staticmethod
    def _reasoning_parameter(model: ModelEntity | None) -> ModelParameter | None:
        """模型不支持推理强度时返回空值，供展示与提交校验一致处理。"""
        if model is None:
            return None
        return next((item for item in model.parameters if item.name == "reasoning_effort"), None)

    @staticmethod
    def with_reasoning_override(config: AppModelConfigReq, effort: str | None) -> AppModelConfigReq:
        """复制调用参数覆盖单个字段，避免改写草稿、发布配置及旧字段别名冲突。"""
        if effort is None:
            return config
        parameters = {key: value for key, value in config.parameters.items()
                      if key not in {"reasoning_effort", "thinking_level", "thinking"}}
        return config.model_copy(update={"parameters": {**parameters, "reasoning_effort": effort}})
