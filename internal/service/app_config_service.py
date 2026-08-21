from dataclasses import dataclass
from typing import Any, Union
from uuid import UUID

from flask import request
from injector import inject
from langchain_core.tools import BaseTool

from internal.core.agent_v2.runtime.subagents import (
    SubAgentSpecError,
    validate_platform_subagents,
)
from internal.core.tools.api_tools.entities import ToolEntity
from internal.core.tools.api_tools.providers import ApiProviderManager
from internal.core.tools.builtin_tools.providers import BuiltinProviderManager
from internal.core.mcp import MCPRuntimeManager
from internal.core.workflow import Workflow as WorkflowTool
from internal.entity.audio_entity import ALLOWED_AUDIO_VOICES
from internal.exception import FailException, ValidateErrorException
from internal.lib.helper import datetime_to_timestamp, get_value_type
from internal.model import (
    App,
    ApiTool,
    MCPServer,
    MCPTool,
    Dataset,
    AppConfig,
    AppConfigVersion,
    AppDatasetJoin,
    ProviderApiKey,
    Workflow,
)
from pkg.sqlalchemy import SQLAlchemy
from .base_service import BaseService
from ..core.language_model import LanguageModelManager
from ..core.language_model.entities.model_entity import ModelEntity, ModelParameterType
from ..core.workflow.entities.workflow_entity import WorkflowConfig
from ..entity.app_entity import AppStatus, DEFAULT_APP_CONFIG
from ..entity.workflow_entity import WorkflowStatus
from .skill_service import SkillService
from .language_model_service import LanguageModelService


@inject
@dataclass
class AppConfigService(BaseService):
    """应用配置服务"""

    db: SQLAlchemy
    api_provider_manager: ApiProviderManager
    builtin_provider_manager: BuiltinProviderManager
    language_model_manager: LanguageModelManager
    language_model_service: LanguageModelService
    mcp_runtime_manager: MCPRuntimeManager
    skill_service: SkillService

    def validate_draft_app_config(
        self,
        draft_app_config: dict[str, Any],
        account_id: UUID,
        app_id: UUID | None = None,
    ) -> dict[str, Any]:
        """校验并标准化可保存的应用草稿配置。"""
        acceptable_fields = {
            "model_config",
            "dialog_round",
            "preset_prompt",
            "skills",
            "subagents",
            "tools",
            "workflows",
            "datasets",
            "retrieval_config",
            "long_term_memory",
            "opening_statement",
            "opening_questions",
            "speech_to_text",
            "text_to_speech",
            "suggested_after_answer",
            "review_config",
        }
        if (
            not isinstance(draft_app_config, dict)
            or not draft_app_config
            or set(draft_app_config) - acceptable_fields
        ):
            raise ValidateErrorException("草稿配置字段出错，请核实后重试")

        normalized = dict(draft_app_config)
        if "model_config" in normalized:
            normalized["model_config"] = self._validate_strict_model_config(
                normalized["model_config"],
                account_id=account_id,
            )

        if "dialog_round" in normalized:
            value = normalized["dialog_round"]
            if not isinstance(value, int) or not 0 <= value <= 100:
                raise ValidateErrorException("携带上下文轮数范围为0-100")

        if "preset_prompt" in normalized:
            value = normalized["preset_prompt"]
            if not isinstance(value, str) or len(value) > 2000:
                raise ValidateErrorException(
                    "人设与回复逻辑必须是字符串，长度在0-2000个字符"
                )

        if "subagents" in normalized:
            try:
                normalized["subagents"] = validate_platform_subagents(
                    normalized["subagents"]
                )
            except SubAgentSpecError as exc:
                raise ValidateErrorException(str(exc)) from exc
            try:
                self.validate_published_agent_references(
                    normalized["subagents"],
                    account_id=account_id,
                    app_id=app_id,
                )
            except SubAgentSpecError as exc:
                raise ValidateErrorException(str(exc)) from exc

        if "skills" in normalized:
            try:
                normalized["skills"] = self.skill_service.normalize_bindings(
                    normalized["skills"]
                )
            except FailException as exc:
                raise ValidateErrorException(str(exc)) from exc

        if "subagents" in normalized:
            enabled_skills = {
                item["name"]
                for item in normalized.get("skills", [])
                if item.get("enabled", True)
            }
            for subagent in normalized["subagents"]:
                missing = sorted(
                    set(subagent.get("skills", [])) - enabled_skills
                )
                if missing:
                    raise ValidateErrorException(
                        "SubAgent 只能选择主 Agent 已挂载的 Skills: "
                        + ", ".join(missing)
                    )

        if "tools" in normalized:
            normalized["tools"] = self._validate_tool_bindings(
                normalized["tools"],
                account_id=account_id,
            )

        if "workflows" in normalized:
            normalized["workflows"] = self._validate_workflow_bindings(
                normalized["workflows"],
                account_id=account_id,
            )

        if "datasets" in normalized:
            normalized["datasets"] = self._validate_dataset_bindings(
                normalized["datasets"],
                account_id=account_id,
            )

        if "retrieval_config" in normalized:
            self._validate_retrieval_config(normalized["retrieval_config"])

        normalized["long_term_memory"] = self._normalize_long_term_memory(
            normalized.get("long_term_memory")
        )

        self._validate_presentation_config(normalized)
        if "review_config" in normalized:
            self._validate_review_config(normalized["review_config"])
        return normalized

    def validate_published_agent_references(
        self,
        subagents: list[dict[str, Any]] | None,
        *,
        account_id: UUID,
        app_id: UUID | None = None,
    ) -> None:
        """校验发布 Agent 引用的租户边界、发布状态和引用环。"""
        for subagent in subagents or []:
            if subagent.get("source", "custom") != "published":
                continue
            target_id = UUID(str(subagent["published_app_id"]))
            self._validate_published_agent_graph(
                target_id,
                account_id=account_id,
                root_app_id=app_id,
                chain=(),
            )

    def _validate_published_agent_graph(
        self,
        app_id: UUID,
        *,
        account_id: UUID,
        root_app_id: UUID | None,
        chain: tuple[UUID, ...],
    ) -> None:
        """递归检查已发布 Agent 的子 Agent 图，阻断自引用和循环引用。"""
        if root_app_id is not None and app_id == root_app_id:
            raise SubAgentSpecError("发布 Agent 不能引用当前应用自身")
        if app_id in chain:
            raise SubAgentSpecError("发布 Agent 子 Agent 引用存在循环")
        if len(chain) >= 32:
            raise SubAgentSpecError("发布 Agent 子 Agent 嵌套层级不能超过 32")

        target = (
            self.db.session.query(App)
            .filter(
                App.id == app_id,
                App.account_id == account_id,
                App.status == AppStatus.PUBLISHED,
            )
            .one_or_none()
        )
        if target is None or target.app_config is None:
            raise SubAgentSpecError(
                f"发布 Agent {app_id} 不存在、未发布或当前账号不可见"
            )

        next_chain = (*chain, app_id)
        for nested in target.app_config.subagents or []:
            if nested.get("source", "custom") != "published":
                continue
            try:
                nested_id = UUID(str(nested["published_app_id"]))
            except (TypeError, ValueError, AttributeError) as exc:
                raise SubAgentSpecError(
                    f"发布 Agent {app_id} 的子 Agent 引用格式错误"
                ) from exc
            self._validate_published_agent_graph(
                nested_id,
                account_id=account_id,
                root_app_id=root_app_id,
                chain=next_chain,
            )

    def _validate_strict_model_config(
        self,
        model_config: Any,
        *,
        account_id: UUID | None = None,
    ) -> dict[str, Any]:
        """严格校验 provider/model，宽松标准化模型参数。"""
        if not isinstance(model_config, dict):
            raise ValidateErrorException("模型配置格式错误，请核实后重试")
        required_keys = {"provider", "model", "parameters"}
        if not required_keys.issubset(model_config) or (
            set(model_config)
            - required_keys
            - {
                "features",
                "modalities",
                "input_modalities",
                "output_modalities",
                "native_file_input",
                "provider_connection_id",
            }
        ):
            raise ValidateErrorException("模型键配置格式错误，请核实后重试")

        provider_name = model_config["provider"]
        model_name = model_config["model"]
        provider_connection_id = model_config.get("provider_connection_id")
        parameters = model_config["parameters"]
        if not isinstance(provider_name, str) or not provider_name:
            raise ValidateErrorException("模型服务提供商类型必须为字符串")
        if not isinstance(model_name, str) or not model_name:
            raise ValidateErrorException("模型名字必须是字符串")
        if not isinstance(parameters, dict):
            raise ValidateErrorException("模型参数配置格式错误")

        try:
            self.language_model_manager.get_provider(provider_name)
        except Exception as exc:
            raise ValidateErrorException("该模型服务提供商不存在，请核实后重试") from exc

        model_entity = self.language_model_service.get_enabled_model_entity(
            provider_name,
            model_name,
            account_id,
            provider_connection_id=provider_connection_id,
        )
        if not model_entity:
            raise ValidateErrorException(
                "该模型未激活，请先在 API 密钥设置中发现并激活后再使用"
            )
        if model_entity.model_type.value != "chat":
            raise ValidateErrorException("Agent 只能选择对话模型")

        normalized_parameters: dict[str, Any] = {}
        for parameter in model_entity.parameters:
            value = parameters.get(parameter.name, parameter.default)
            if value is not None and get_value_type(value) != parameter.type.value:
                value = parameter.default
            if parameter.required and value is None:
                value = parameter.default
            if parameter.options and value not in parameter.options:
                value = parameter.default
            if (
                parameter.type in [ModelParameterType.INT, ModelParameterType.FLOAT]
                and value is not None
                and (
                    (parameter.min is not None and value < parameter.min)
                    or (parameter.max is not None and value > parameter.max)
                )
            ):
                value = parameter.default
            normalized_parameters[parameter.name] = value

        return {
            "provider": provider_name,
            "model": model_name,
            "provider_connection_id": str(
                model_entity.attributes.get("provider_api_key_id") or ""
            ),
            "parameters": normalized_parameters,
            "features": [feature.value for feature in model_entity.features],
            "input_modalities": [
                modality.value for modality in model_entity.input_modalities
            ],
            "output_modalities": [
                modality.value for modality in model_entity.output_modalities
            ],
            "native_file_input": model_entity.native_file_input,
        }

    @staticmethod
    def _normalize_long_term_memory(value: Any) -> dict[str, bool]:
        """长期记忆是 Agent 基础能力，历史关闭值只允许被修正为开启。"""
        if value is not None and not isinstance(value, dict):
            raise ValidateErrorException("长期记忆配置格式错误")
        if (
            isinstance(value, dict)
            and "enable" in value
            and not isinstance(value["enable"], bool)
        ):
            raise ValidateErrorException("长期记忆配置格式错误")
        return {"enable": True}

    def _validate_tool_bindings(
        self,
        tools: Any,
        *,
        account_id: UUID,
    ) -> list[dict[str, Any]]:
        """校验工具引用并剔除当前租户不可见的工具。"""
        if not isinstance(tools, list):
            raise ValidateErrorException("工具列表必须是列表型数据")
        if len(tools) > 5:
            raise ValidateErrorException("Agent绑定的工具数不能超过5")

        valid: list[dict[str, Any]] = []
        for tool in tools:
            if (
                not isinstance(tool, dict)
                or set(tool) != {"type", "provider_id", "tool_id", "params"}
                or tool["type"] not in {"builtin_tool", "api_tool", "mcp_tool"}
                or not isinstance(tool["provider_id"], str)
                or not isinstance(tool["tool_id"], str)
                or not tool["provider_id"]
                or not tool["tool_id"]
                or not isinstance(tool["params"], dict)
            ):
                raise ValidateErrorException("绑定插件工具参数出错")

            tool_type = tool["type"]
            if tool_type == "builtin_tool":
                exists = self.builtin_provider_manager.get_tool(
                    tool["provider_id"],
                    tool["tool_id"],
                )
            elif tool_type == "api_tool":
                exists = (
                    self.db.session.query(ApiTool)
                    .filter(
                        ApiTool.provider_id == tool["provider_id"],
                        ApiTool.name == tool["tool_id"],
                        ApiTool.account_id == account_id,
                    )
                    .one_or_none()
                )
            else:
                server = (
                    self.db.session.query(MCPServer)
                    .filter(
                        MCPServer.id == tool["provider_id"],
                        MCPServer.account_id == account_id,
                        MCPServer.enabled.is_(True),
                    )
                    .one_or_none()
                )
                exists = (
                    self.db.session.query(MCPTool)
                    .filter(
                        MCPTool.server_id == getattr(server, "id", None),
                        MCPTool.name == tool["tool_id"],
                        MCPTool.account_id == account_id,
                    )
                    .one_or_none()
                    if server
                    else None
                )
            if exists:
                valid.append(dict(tool))

        keys = [(item["provider_id"], item["tool_id"]) for item in valid]
        if len(keys) != len(set(keys)):
            raise ValidateErrorException("绑定插件存在重复")
        return valid

    def _validate_workflow_bindings(
        self,
        workflows: Any,
        *,
        account_id: UUID,
    ) -> list[str]:
        """校验已发布且属于当前租户的工作流引用。"""
        if not isinstance(workflows, list):
            raise ValidateErrorException("绑定工作流列表参数格式错误")
        if len(workflows) > 5:
            raise ValidateErrorException("Agent绑定的工作流数量不能超过5个")

        normalized: list[str] = []
        for workflow_id in workflows:
            try:
                normalized.append(str(UUID(str(workflow_id))))
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValidateErrorException("工作流参数必须是UUID") from exc
        if len(normalized) != len(set(normalized)):
            raise ValidateErrorException("绑定工作流存在重复")

        records = (
            self.db.session.query(Workflow)
            .filter(
                Workflow.id.in_(normalized),
                Workflow.account_id == account_id,
                Workflow.status == WorkflowStatus.PUBLISHED,
            )
            .all()
        )
        visible = {str(record.id) for record in records}
        return [workflow_id for workflow_id in normalized if workflow_id in visible]

    def _validate_dataset_bindings(
        self,
        datasets: Any,
        *,
        account_id: UUID,
    ) -> list[str]:
        """校验属于当前租户的知识库引用。"""
        if not isinstance(datasets, list):
            raise ValidateErrorException("绑定知识库列表参数格式错误")
        if len(datasets) > 5:
            raise ValidateErrorException("Agent绑定的知识库数量不能超过5个")

        normalized: list[str] = []
        for dataset_id in datasets:
            try:
                normalized.append(str(UUID(str(dataset_id))))
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValidateErrorException("知识库列表参数必须是UUID") from exc
        if len(normalized) != len(set(normalized)):
            raise ValidateErrorException("绑定知识库存在重复")

        records = (
            self.db.session.query(Dataset)
            .filter(
                Dataset.id.in_(normalized),
                Dataset.account_id == account_id,
            )
            .all()
        )
        visible = {str(record.id) for record in records}
        return [dataset_id for dataset_id in normalized if dataset_id in visible]

    @staticmethod
    def _validate_retrieval_config(value: Any) -> None:
        """校验知识库检索参数。"""
        if (
            not isinstance(value, dict)
            or set(value) != {"retrieval_strategy", "k", "score"}
            or value["retrieval_strategy"] not in {"semantic", "full_text", "hybrid"}
            or not isinstance(value["k"], int)
            or not 0 <= value["k"] <= 10
            or not isinstance(value["score"], (int, float))
            or not 0 <= value["score"] <= 1
        ):
            raise ValidateErrorException("检索配置格式错误")

    @staticmethod
    def _validate_presentation_config(config: dict[str, Any]) -> None:
        """校验不参与 Agent Prompt 的前端展示配置。"""
        if "opening_statement" in config and (
            not isinstance(config["opening_statement"], str)
            or len(config["opening_statement"]) > 2000
        ):
            raise ValidateErrorException("对话开场白的长度范围是0-2000")

        if "opening_questions" in config:
            questions = config["opening_questions"]
            if (
                not isinstance(questions, list)
                or len(questions) > 3
                or any(not isinstance(item, str) for item in questions)
            ):
                raise ValidateErrorException("开场建议问题格式错误")

        if "speech_to_text" in config:
            value = config["speech_to_text"]
            if (
                not isinstance(value, dict)
                or set(value) != {"enable"}
                or not isinstance(value["enable"], bool)
            ):
                raise ValidateErrorException("语音转文本设置格式错误")

        if "text_to_speech" in config:
            value = config["text_to_speech"]
            if (
                not isinstance(value, dict)
                or set(value) != {"enable", "voice", "auto_play"}
                or not isinstance(value["enable"], bool)
                or value["voice"] not in ALLOWED_AUDIO_VOICES
                or not isinstance(value["auto_play"], bool)
            ):
                raise ValidateErrorException("文本转语音设置格式错误")

        if "suggested_after_answer" in config:
            value = config["suggested_after_answer"]
            if (
                not isinstance(value, dict)
                or set(value) != {"enable"}
                or not isinstance(value["enable"], bool)
            ):
                raise ValidateErrorException("回答后建议问题设置格式错误")

    @staticmethod
    def _validate_review_config(value: Any) -> None:
        """校验输入输出审核配置。"""
        if (
            not isinstance(value, dict)
            or set(value) != {"enable", "keywords", "inputs_config", "outputs_config"}
            or not isinstance(value["enable"], bool)
            or not isinstance(value["keywords"], list)
            or len(value["keywords"]) > 100
            or any(not isinstance(item, str) for item in value["keywords"])
        ):
            raise ValidateErrorException("审核配置格式错误")

        inputs = value["inputs_config"]
        outputs = value["outputs_config"]
        if (
            not isinstance(inputs, dict)
            or set(inputs) != {"enable", "preset_response"}
            or not isinstance(inputs["enable"], bool)
            or not isinstance(inputs["preset_response"], str)
            or not isinstance(outputs, dict)
            or set(outputs) != {"enable"}
            or not isinstance(outputs["enable"], bool)
        ):
            raise ValidateErrorException("审核配置格式错误")
        if value["enable"]:
            if not value["keywords"]:
                raise ValidateErrorException(
                    "review.keywords非空且不能超过100个关键词"
                )
            if not inputs["enable"] and not outputs["enable"]:
                raise ValidateErrorException("输入审核和输出审核至少需要开启一项")
            if inputs["enable"] and not inputs["preset_response"].strip():
                raise ValidateErrorException("输入审核预设响应不能为空")

    def get_draft_app_config(self, app: App) -> dict[str, Any]:
        """根据传递的应用获取该应用的草稿配置"""
        # 1.提取应用的草稿配置
        draft_app_config = app.draft_app_config

        normalized_long_term_memory = self._normalize_long_term_memory(
            draft_app_config.long_term_memory
        )
        if draft_app_config.long_term_memory != normalized_long_term_memory:
            self.update(
                draft_app_config,
                long_term_memory=normalized_long_term_memory,
            )

        # 2.校验model_config信息，如果使用了不存在的提供者或者模型，则使用默认值(宽松校验)
        validate_model_config = self._process_and_validate_model_config(
            draft_app_config.model_config,
            account_id=app.account_id,
        )
        if draft_app_config.model_config != validate_model_config:
            self.update(draft_app_config, model_config=validate_model_config)

        # 3.循环遍历工具列表删除已经被删除的工具信息
        tools, validate_tools = self._process_and_validate_tools(
            draft_app_config.tools,
            account_id=app.account_id,
        )

        # 4.判断是否需要更新草稿配置中的工具列表信息
        if draft_app_config.tools != validate_tools:
            # 14.更新草稿配置中的工具列表
            self.update(draft_app_config, tools=validate_tools)

        # 5.校验知识库列表，如果引用了不存在/被删除的知识库，需要剔除数据并更新，同时获取知识库的额外信息
        datasets, validate_datasets = self._process_and_validate_datasets(
            draft_app_config.datasets,
            account_id=app.account_id,
        )

        # 6.判断是否存在已删除的知识库，如果存在则更新
        if set(validate_datasets) != set(draft_app_config.datasets):
            self.update(draft_app_config, datasets=validate_datasets)

        # 7.校验工作流列表对应的数据
        workflows, validate_workflows = self._process_and_validate_workflows(
            draft_app_config.workflows,
            account_id=app.account_id,
        )
        if set(validate_workflows) != set(draft_app_config.workflows):
            self.update(draft_app_config, workflows=validate_workflows)
        # 20.将数据转换成字典后返回
        return self._process_and_transformer_app_config(
            validate_model_config,
            tools,
            workflows,
            datasets,
            draft_app_config,
        )

    def get_app_config(self, app: App) -> dict[str, Any]:
        """根据传递的应用获取该应用的运行配置"""
        # 1.提取应用的草稿配置
        app_config = app.app_config

        normalized_long_term_memory = self._normalize_long_term_memory(
            app_config.long_term_memory
        )
        if app_config.long_term_memory != normalized_long_term_memory:
            self.update(
                app_config,
                long_term_memory=normalized_long_term_memory,
            )

        # 2.校验model_config信息，如果运行时配置里的model_config发生变化则进行更新
        validate_model_config = self._process_and_validate_model_config(
            app_config.model_config,
            account_id=app.account_id,
        )
        if app_config.model_config != validate_model_config:
            self.update(app_config, model_config=validate_model_config)

        # 3.循环遍历工具列表删除已经被删除的工具信息
        tools, validate_tools = self._process_and_validate_tools(
            app_config.tools,
            account_id=app.account_id,
        )

        # 4.判断是否需要更新草稿配置中的工具列表信息
        if app_config.tools != validate_tools:
            # 14.更新草稿配置中的工具列表
            self.update(app_config, tools=validate_tools)

        # 5.校验知识库列表，如果引用了不存在/被删除的知识库，需要剔除数据并更新，同时获取知识库的额外信息
        app_dataset_joins = app_config.app_dataset_joins
        origin_datasets = [
            str(app_dataset_join.dataset_id) for app_dataset_join in app_dataset_joins
        ]
        datasets, validate_datasets = self._process_and_validate_datasets(
            origin_datasets,
            account_id=app.account_id,
        )

        # 6.判断是否存在已删除的知识库，如果存在则更新
        for dataset_id in set(origin_datasets) - set(validate_datasets):
            with self.db.auto_commit():
                self.db.session.query(AppDatasetJoin).filter(
                    AppDatasetJoin.app_id == app.id,
                    AppDatasetJoin.dataset_id == dataset_id
                ).delete()

        # 7.校验工作流列表对应的数据
        workflows, validate_workflows = self._process_and_validate_workflows(
            app_config.workflows,
            account_id=app.account_id,
        )
        if set(validate_workflows) != set(app_config.workflows):
            self.update(app_config, workflows=validate_workflows)

        # 20.将数据转换成字典后返回
        return self._process_and_transformer_app_config(
            validate_model_config,
            tools,
            workflows,
            datasets,
            app_config,
        )

    def get_langchain_tools_by_tools_config(
        self,
        tools_config: list[dict],
        *,
        account_id: UUID,
    ) -> list[BaseTool]:
        """根据传递的工具配置列表获取langchain工具列表"""
        # 1.循环遍历所有工具配置列表信息
        tools = []
        for tool in tools_config:
            # 2.根据不同的工具类型执行不同的操作
            if tool["type"] == "builtin_tool":
                # 3.内置工具，通过builtin_provider_manager获取工具实例
                builtin_tool = self.builtin_provider_manager.get_tool(
                    tool["provider"]["id"], tool["tool"]["name"]
                )
                if not builtin_tool:
                    continue
                tools.append(builtin_tool(**tool["tool"]["params"]))
            elif tool["type"] == "mcp_tool":
                server = (
                    self.db.session.query(MCPServer)
                    .filter(
                        MCPServer.id == tool["provider"]["id"],
                        MCPServer.account_id == account_id,
                    )
                    .one_or_none()
                )
                if not server or not server.enabled:
                    continue
                try:
                    tools.append(
                        self.mcp_runtime_manager.get_langchain_tool(
                            server, tool["tool"]["name"]
                        )
                    )
                except Exception:
                    continue
            else:
                # 4.API工具，首先根据id找到ApiTool记录，然后创建示例
                api_tool = (
                    self.db.session.query(ApiTool)
                    .filter(
                        ApiTool.id == tool["tool"]["id"],
                        ApiTool.account_id == account_id,
                    )
                    .one_or_none()
                )
                if not api_tool:
                    continue
                tools.append(
                    self.api_provider_manager.get_tool(
                        ToolEntity(
                            id=str(api_tool.id),
                            name=api_tool.name,
                            url=api_tool.url,
                            method=api_tool.method,
                            description=api_tool.description,
                            headers=api_tool.provider.headers,
                            parameters=api_tool.parameters,
                        )
                    )
                )

        return tools

    def get_langchain_tools_by_workflow_ids(
        self,
        workflow_ids: list[UUID],
        *,
        account_id: UUID,
    ) -> list[BaseTool]:
        """根据传递的工作流配置列表获取langchain工具列表"""
        # 1.根据传递的工作流id查询工作流记录信息
        workflow_records = (
            self.db.session.query(Workflow)
            .filter(
                Workflow.id.in_(workflow_ids),
                Workflow.account_id == account_id,
                Workflow.status == WorkflowStatus.PUBLISHED,
            )
            .all()
        )

        # 2.循环遍历所有工作流记录列表
        workflows = []
        for workflow_record in workflow_records:
            try:
                # 3.创建工作流工具
                workflow_tool = WorkflowTool(
                    workflow_config=WorkflowConfig(
                        account_id=workflow_record.account_id,
                        name=f"wf_{workflow_record.tool_call_name}",
                        description=workflow_record.description,
                        nodes=workflow_record.graph.get("nodes", []),
                        edges=workflow_record.graph.get("edges", []),
                    )
                )
                workflows.append(workflow_tool)
            except Exception:
                continue

        return workflows

    @classmethod
    def _process_and_transformer_app_config(
        cls,
        model_config: dict[str, Any],
        tools: list[dict],
        workflows: list[dict],
        datasets: list[dict],
        app_config: Union[AppConfig, AppConfigVersion],
    ) -> dict[str, Any]:
        """根据传递的插件列表、工作流列表、知识库列表以及应用配置创建字典信息"""
        return {
            "id": str(app_config.id),
            "model_config": model_config,
            "dialog_round": app_config.dialog_round,
            "preset_prompt": app_config.preset_prompt,
            "skills": getattr(app_config, "skills", []) or [],
            "subagents": app_config.subagents or [],
            "tools": tools,
            "workflows": workflows,
            "datasets": datasets,
            "retrieval_config": app_config.retrieval_config,
            "long_term_memory": cls._normalize_long_term_memory(
                getattr(app_config, "long_term_memory", None)
            ),
            "opening_statement": app_config.opening_statement,
            "opening_questions": app_config.opening_questions,
            "speech_to_text": app_config.speech_to_text,
            "text_to_speech": app_config.text_to_speech,
            "suggested_after_answer": app_config.suggested_after_answer,
            "review_config": app_config.review_config,
            "updated_at": datetime_to_timestamp(app_config.updated_at),
            "created_at": datetime_to_timestamp(app_config.created_at),
        }

    def _process_and_validate_tools(
        self,
        origin_tools: list[dict],
        *,
        account_id: UUID,
    ) -> tuple[list[dict], list[dict]]:
        """根据传递的原始工具信息进行处理和校验"""
        # 1.循环遍历工具列表删除已被删除的工具
        validate_tools = []
        tools = []
        for tool in origin_tools:
            if tool["type"] == "builtin_tool":
                # 2.查询内置工具提供者，并检测是否存在
                provider = self.builtin_provider_manager.get_provider(
                    tool["provider_id"]
                )
                if not provider:
                    continue

                # 3.获取提供者下的工具实体，并检测是否存在
                tool_entity = provider.get_tool_entity(tool["tool_id"])
                if not tool_entity:
                    continue

                # 4.判断工具的params和草稿中的params是否一致，如果不一致则全部重置为默认值（或者考虑删除这个工具的引用）
                param_keys = set([param.name for param in tool_entity.params])
                params = tool["params"]
                if set(tool["params"].keys()) - param_keys:
                    params = {
                        param.name: param.default
                        for param in tool_entity.params
                        if param.default is not None
                    }

                # 5.数据都存在，并且参数已经校验完毕，可以将数据添加到validate_tools
                validate_tools.append({**tool, "params": params})

                # 6.组装内置工具展示信息
                provider_entity = provider.provider_entity
                tools.append(
                    {
                        "type": "builtin_tool",
                        "provider": {
                            "id": provider_entity.name,
                            "name": provider_entity.name,
                            "label": provider_entity.label,
                            "icon": f"{request.scheme}://{request.host}/builtin-tools/{provider_entity.name}/icon",
                            "description": provider_entity.description,
                        },
                        "tool": {
                            "id": tool_entity.name,
                            "name": tool_entity.name,
                            "label": tool_entity.label,
                            "description": tool_entity.description,
                            "params": tool["params"],
                        },
                    }
                )
            elif tool["type"] == "api_tool":
                # 7.查询数据库获取对应的工具记录，并检测是否存在
                tool_record = (
                    self.db.session.query(ApiTool)
                    .filter(
                        ApiTool.provider_id == tool["provider_id"],
                        ApiTool.name == tool["tool_id"],
                        ApiTool.account_id == account_id,
                    )
                    .one_or_none()
                )
                if not tool_record:
                    continue

                # 8.数据校验通过，往validate_tools中添加数据
                validate_tools.append(tool)

                # 9.组装api工具展示信息
                provider = tool_record.provider
                tools.append(
                    {
                        "type": "api_tool",
                        "provider": {
                            "id": str(provider.id),
                            "name": provider.name,
                            "label": provider.name,
                            "icon": provider.icon,
                            "description": provider.description,
                        },
                        "tool": {
                            "id": str(tool_record.id),
                            "name": tool_record.name,
                            "label": tool_record.name,
                            "description": tool_record.description,
                            "params": {},
                        },
                    }
                )
            elif tool["type"] == "mcp_tool":
                server = (
                    self.db.session.query(MCPServer)
                    .filter(
                        MCPServer.id == tool["provider_id"],
                        MCPServer.account_id == account_id,
                    )
                    .one_or_none()
                )
                if not server or not server.enabled:
                    continue

                tool_record = (
                    self.db.session.query(MCPTool)
                    .filter(
                        MCPTool.server_id == server.id,
                        MCPTool.name == tool["tool_id"],
                        MCPTool.account_id == account_id,
                    )
                    .one_or_none()
                )
                if not tool_record:
                    continue

                validate_tools.append({**tool, "params": {}})
                tools.append(
                    {
                        "type": "mcp_tool",
                        "provider": {
                            "id": str(server.id),
                            "name": server.name,
                            "label": server.name,
                            "icon": "",
                            "description": server.description,
                        },
                        "tool": {
                            "id": str(tool_record.id),
                            "name": tool_record.name,
                            "label": tool_record.name,
                            "description": tool_record.description,
                            "params": {},
                        },
                    }
                )

        return tools, validate_tools

    def _process_and_validate_datasets(
        self,
        origin_datasets: list[dict],
        *,
        account_id: UUID,
    ) -> tuple[list[dict], list[dict]]:
        """根据传递的知识库并返回知识库配置与校验后的数据"""
        # 1.校验知识库配置列表，如果引用了不存在的/被删除的知识库，则需要剔除数据并更新，同时获取知识库的额外信息
        datasets = []
        dataset_records = (
            self.db.session.query(Dataset)
            .filter(
                Dataset.id.in_(origin_datasets),
                Dataset.account_id == account_id,
            )
            .all()
        )
        dataset_dict = {
            str(dataset_record.id): dataset_record for dataset_record in dataset_records
        }
        dataset_sets = set(dataset_dict.keys())
        embedding_fingerprints = {
            dataset.embedding_fingerprint for dataset in dataset_records
        }
        if len(embedding_fingerprints) > 1:
            raise ValidateErrorException("关联知识库必须使用相同的向量模型")

        # 2.计算存在的知识库id列表，为了保留原始顺序，使用列表循环的方式来判断
        validate_datasets = [
            dataset_id for dataset_id in origin_datasets if dataset_id in dataset_sets
        ]

        # 3.循环获取知识库数据
        for dataset_id in validate_datasets:
            dataset = dataset_dict.get(str(dataset_id))
            datasets.append(
                {
                    "id": str(dataset.id),
                    "name": dataset.name,
                    "icon": dataset.icon,
                    "description": dataset.description,
                    "embedding_provider": dataset.embedding_provider,
                    "embedding_model": dataset.embedding_model,
                    "embedding_fingerprint": dataset.embedding_fingerprint,
                }
            )

        return datasets, validate_datasets

    def _process_and_validate_model_config(
        self,
        origin_model_config: dict[str, Any],
        *,
        account_id: UUID | None = None,
    ) -> dict[str, Any]:
        """根据传递的模型配置处理并校验，随后返回校验后的信息"""
        def default_active_model_config() -> dict[str, Any]:
            return self.language_model_service.get_default_model_config(account_id)

        # 1.判断model_config是否为字典，如果不是则直接返回默认值
        if not isinstance(origin_model_config, dict):
            return default_active_model_config()

        # 2.提取origin_model_config中provider、model、parameters对应的信息
        model_config = {
            "provider": origin_model_config.get("provider", ""),
            "model": origin_model_config.get("model", ""),
            "provider_connection_id": origin_model_config.get(
                "provider_connection_id", ""
            ),
            "parameters": origin_model_config.get("parameters", {}),
        }

        # 3.判断provider是否存在、类型是否正确，如果不符合规则则返回默认值
        if not model_config["provider"] or not isinstance(
            model_config["provider"], str
        ):
            return default_active_model_config()
        try:
            self.language_model_manager.get_provider(model_config["provider"])
        except Exception:
            return default_active_model_config()

        # 4.判断model是否存在、类型是否正确，如果不符合则返回默认值
        if not model_config["model"] or not isinstance(model_config["model"], str):
            return default_active_model_config()
        model_entity = self.language_model_service.get_enabled_model_entity(
            model_config["provider"],
            model_config["model"],
            account_id,
            provider_connection_id=model_config["provider_connection_id"],
        )
        if not model_entity:
            return default_active_model_config()
        if model_entity.model_type.value != "chat":
            return default_active_model_config()

        # 5.判断parameters信息类型是否错误，如果错误则设置为默认值
        if not isinstance(model_config["parameters"], dict):
            model_config["parameters"] = {
                parameter.name: parameter.default
                for parameter in model_entity.parameters
            }

        # 6.剔除传递的多余的parameter，亦或者是少传递的参数使用默认值补上
        parameters = {}
        for parameter in model_entity.parameters:
            # 7.从model_config中获取参数值，如果不存在则设置为默认值
            parameter_value = model_config["parameters"].get(
                parameter.name, parameter.default
            )

            # 8.判断参数是否必填
            if parameter.required:
                # 9.参数必填，则值不允许为None，如果为None则设置默认值
                if parameter_value is None:
                    parameter_value = parameter.default
                else:
                    # 10.值非空则校验数据类型是否正确，不正确则设置默认值
                    if get_value_type(parameter_value) != parameter.type.value:
                        parameter_value = parameter.default
            else:
                # 11.参数非必填，数据非空的情况下需要校验
                if parameter_value is not None:
                    if get_value_type(parameter_value) != parameter.type.value:
                        parameter_value = parameter.default

            # 12.判断参数是否存在options，如果存在则数值必须在options中选择
            if parameter.options and parameter_value not in parameter.options:
                parameter_value = parameter.default

            # 13.参数类型为int/float，如果存在min/max时候需要校验
            if (
                parameter.type in [ModelParameterType.INT, ModelParameterType.FLOAT]
                and parameter_value is not None
            ):
                # 14.校验数值的min/max
                if (parameter.min and parameter_value < parameter.min) or (
                    parameter.max and parameter_value > parameter.max
                ):
                    parameter_value = parameter.default

            parameters[parameter.name] = parameter_value

        # 15.完成数据校验，赋值parameters参数
        model_config["parameters"] = parameters
        model_config["provider_connection_id"] = str(
            model_entity.attributes.get("provider_api_key_id") or ""
        )

        # 16.能力字段始终从当前已激活模型快照派生，避免应用保存过期的发现结果。
        model_config["features"] = [f.value for f in model_entity.features]
        model_config["input_modalities"] = [
            modality.value for modality in model_entity.input_modalities
        ]
        model_config["output_modalities"] = [
            modality.value for modality in model_entity.output_modalities
        ]
        model_config["native_file_input"] = model_entity.native_file_input

        return model_config

    def _get_enabled_discovered_models(
        self,
        provider_name: str,
        account_id: UUID | None,
    ) -> list[ModelEntity]:
        """兼容旧调用名，统一读取当前账号已激活的模型。"""
        return self.language_model_service.get_enabled_model_entities(
            provider_name,
            account_id,
        )

    def _process_and_validate_workflows(
        self,
        origin_workflows: list[UUID],
        *,
        account_id: UUID,
    ) -> tuple[list[dict], list[UUID]]:
        """根据传递的工作流列表并返回工作流配置和校验后的数据"""
        # 1.校验工作流配置列表，如果引用了不存在/被删除的工作流，则需要提出数据并更新，同时获取工作流的额外信息
        workflows = []
        workflow_records = (
            self.db.session.query(Workflow)
            .filter(
                Workflow.id.in_(origin_workflows),
                Workflow.account_id == account_id,
                Workflow.status == WorkflowStatus.PUBLISHED,
            )
            .all()
        )
        workflow_dict = {
            str(workflow_record.id): workflow_record
            for workflow_record in workflow_records
        }
        workflow_sets = set(workflow_dict.keys())

        # 2.计算存在的工作流id列表，为了保留原始顺序，使用列表循环的方式来判断
        validate_workflows = [
            workflow_id
            for workflow_id in origin_workflows
            if workflow_id in workflow_sets
        ]

        # 3.循环获取工作流数据
        for workflow_id in validate_workflows:
            workflow = workflow_dict.get(str(workflow_id))
            workflows.append(
                {
                    "id": str(workflow.id),
                    "name": workflow.name,
                    "icon": workflow.icon,
                    "description": workflow.description,
                }
            )

        return workflows, validate_workflows
