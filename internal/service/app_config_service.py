from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast, Union
from uuid import UUID

from injector import inject
from langchain_core.tools import BaseTool
from pydantic import ValidationError

from internal.core.agent_v2.entity.subagent_config import (
    SubAgentConfig,
    SubAgentConfigList,
)
from internal.core.tools.api_tools.entities import ToolEntity
from internal.core.tools.api_tools.providers import ApiProviderManager
from internal.core.tools.builtin_tools.providers import BuiltinProviderManager
from internal.core.mcp import MCPRuntimeManager
from internal.core.workflow import Workflow as WorkflowTool
from internal.exception import (
    NotFoundException,
    ValidateErrorException,
)
from internal.lib.helper import datetime_to_timestamp
from internal.core.language_model.parameters import validate_model_parameters
from internal.model import (
    App,
    ApiTool,
    MCPServer,
    MCPTool,
    Dataset,
    AppConfig,
    AppConfigVersion,
    AppDatasetJoin,
    Workflow,
)
from internal.schema.app_config_schema import (
    AgentAppConfigReq,
    AppModelConfigReq,
    AppToolBindingReq,
    AppToolBindingListReq,
    DraftAppConfigPatchReq,
    LongTermMemoryConfigReq,
)
from pkg.sqlalchemy import SQLAlchemy
from .base_service import BaseService
from ..core.language_model import LanguageModelManager
from ..core.workflow.entities.workflow_entity import WorkflowConfig
from ..entity.app_entity import AppStatus, DEFAULT_APP_CONFIG
from ..entity.workflow_entity import WorkflowStatus
from .skill_service import SkillService
from .language_model_service import LanguageModelService


@inject
@dataclass
class AppConfigService(BaseService):
    """负责 AppConfig 的业务校验、标准化和能力绑定转换。

    本服务校验 App、租户、发布状态以及数据库引用；纯 Agent 图构建
    和执行不属于本服务。
    """

    db: SQLAlchemy
    api_provider_manager: ApiProviderManager
    builtin_provider_manager: BuiltinProviderManager
    language_model_manager: LanguageModelManager
    language_model_service: LanguageModelService
    mcp_runtime_manager: MCPRuntimeManager
    skill_service: SkillService

    def validate_draft_app_config(
        self,
        draft_app_config: DraftAppConfigPatchReq,
        account_id: UUID,
        app_id: UUID | None = None,
        current_config: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """补充数据库业务校验，并返回可持久化的草稿更新数据。"""
        normalized = draft_app_config.model_dump(
            mode="json",
            by_alias=True,
            exclude_unset=True,
            exclude_none=True,
        )

        # 结构和字段组合已经由 DraftAppConfigPatchReq 校验；这里只查询业务资源。
        if draft_app_config.model_config_data is not None:
            normalized["model_config"] = self._validate_strict_model_config(
                draft_app_config.model_config_data,
                account_id=account_id,
            )

        if draft_app_config.subagents is not None:
            normalized["subagents"] = self._normalize_subagent_configs(
                draft_app_config.subagents
            )
            for subagent in normalized["subagents"]:
                if not subagent.get("model"):
                    continue
                model_config = self._validate_strict_model_config(
                    AppModelConfigReq(
                        provider=subagent["model_provider"],
                        model=subagent["model"],
                        provider_connection_id=subagent.get("model_connection_id"),
                        parameters={},
                    ),
                    account_id=account_id,
                )
                # 固定本次授权的连接，避免以后新增同名连接改变子 Agent 的调用目标。
                if model_config["provider_connection_id"]:
                    subagent["model_connection_id"] = model_config["provider_connection_id"]
            self.validate_published_agent_references(
                draft_app_config.subagents,
                account_id=account_id,
                app_id=app_id,
            )

        if draft_app_config.skills is not None:
            normalized["skills"] = self.skill_service.normalize_bindings(
                draft_app_config.skills
            )

        if draft_app_config.subagents is not None or draft_app_config.skills is not None:
            # Patch 未提交的关联字段沿用草稿；仅用合并视图校验，不扩大写入范围。
            effective_config = {**(current_config or {}), **normalized}
            enabled_skills = {
                item["name"]
                for item in effective_config.get("skills", [])
                if item.get("enabled", True)
            }
            for subagent in effective_config.get("subagents", []):
                missing = sorted(set(subagent.get("skills", [])) - enabled_skills)
                if missing:
                    raise ValidateErrorException(
                        "SubAgent 只能选择主 Agent 已挂载的 Skills: "
                        + ", ".join(missing)
                    )

        if draft_app_config.tools is not None:
            normalized["tools"] = self._validate_tool_bindings(
                draft_app_config.tools.root,
                account_id=account_id,
            )

        if draft_app_config.workflows is not None:
            normalized["workflows"] = self._validate_workflow_bindings(
                draft_app_config.workflows.root,
                account_id=account_id,
            )

        if draft_app_config.datasets is not None:
            normalized["datasets"] = self._validate_dataset_bindings(
                draft_app_config.datasets.root,
                account_id=account_id,
            )
        return normalized

    @staticmethod
    def _parse_subagent_configs(
        value: Any,
    ) -> list[SubAgentConfig]:
        """解析 SubAgent 配置，并统一映射字段级校验错误。"""
        try:
            return SubAgentConfigList.model_validate(
                [] if value is None else value
            ).root
        except ValidationError as exc:
            raise ValidateErrorException(str(exc)) from exc

    @classmethod
    def _normalize_subagent_configs(
        cls,
        value: Sequence[SubAgentConfig] | Any,
    ) -> list[dict[str, Any]]:
        """将已校验的 SubAgent 配置转换为持久化字典。"""
        normalized: list[dict[str, Any]] = []
        for config in cls._parse_subagent_configs(value):
            item = {
                "name": config.name,
                "description": config.description,
                "mode": config.mode,
                "context_passing": config.context_passing,
                "tools": list(config.tools),
                "skills": list(config.skills),
            }
            if config.mode == "async":
                item.update(
                    {
                        "source": config.source,
                        "graph_id": config.graph_id,
                        "headers": dict(config.headers),
                    }
                )
                if config.source == "published":
                    item["published_app_id"] = str(config.published_app_id)
                # DeepAgents 的 AsyncSubAgent 将 url 作为可选 transport 配置；
                # 未配置时省略字段，由运行时按来源解析内部或外部 transport。
                if config.url is not None:
                    item["url"] = config.url
            else:
                item["system_prompt"] = config.system_prompt
                if config.source == "published":
                    item.update(
                        {
                            "source": config.source,
                            "published_app_id": str(config.published_app_id),
                        }
                    )
                elif config.model is not None:
                    item["model"] = config.model
                    item["model_provider"] = config.model_provider
                    if config.model_connection_id is not None:
                        item["model_connection_id"] = str(config.model_connection_id)
            normalized.append(item)
        return normalized

    def validate_published_agent_references(
        self,
        subagents: Sequence[SubAgentConfig] | Any,
        *,
        account_id: UUID,
        app_id: UUID | None = None,
    ) -> None:
        """校验发布 Agent 引用的租户边界、发布状态和引用环。"""
        for subagent in self._parse_subagent_configs(subagents):
            if subagent.source != "published":
                continue
            target_id = cast(UUID, subagent.published_app_id)
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
            raise ValidateErrorException("发布 Agent 不能引用当前应用自身")
        if app_id in chain:
            raise ValidateErrorException("发布 Agent 子 Agent 引用存在循环")
        if len(chain) >= 32:
            raise ValidateErrorException("发布 Agent 子 Agent 嵌套层级不能超过 32")

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
            raise ValidateErrorException(
                f"发布 Agent {app_id} 不存在、未发布或当前账号不可见"
            )

        next_chain = (*chain, app_id)
        for nested in self._parse_subagent_configs(target.app_config.subagents):
            if nested.source != "published":
                continue
            nested_id = cast(UUID, nested.published_app_id)
            self._validate_published_agent_graph(
                nested_id,
                account_id=account_id,
                root_app_id=root_app_id,
                chain=next_chain,
            )

    def _validate_strict_model_config(
        self,
        model_config: AppModelConfigReq,
        *,
        account_id: UUID | None = None,
    ) -> dict[str, Any]:
        """校验当前租户是否可以使用结构已经合法的对话模型。"""
        provider_name = model_config.provider
        model_name = model_config.model
        try:
            self.language_model_manager.get_provider(provider_name)
        except NotFoundException as exc:
            raise ValidateErrorException(
                "该模型服务提供商不存在，请核实后重试"
            ) from exc

        model_entity = self.language_model_service.get_enabled_model_entity(
            provider_name,
            model_name,
            account_id,
            provider_connection_id=(
                str(model_config.provider_connection_id)
                if model_config.provider_connection_id is not None
                else None
            ),
        )
        if not model_entity:
            raise ValidateErrorException(
                "该模型未激活，请先在 API 密钥设置中发现并激活后再使用"
            )
        if model_entity.model_type.value != "chat":
            raise ValidateErrorException("Agent 只能选择对话模型")

        try:
            normalized_parameters = validate_model_parameters(model_entity, model_config.parameters)
        except ValueError as exc:
            raise ValidateErrorException(str(exc)) from exc

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
        """解析持久化的长期记忆配置并返回稳定 JSON 结构。"""
        try:
            config = LongTermMemoryConfigReq.model_validate(value or {})
        except ValidationError as exc:
            raise ValidateErrorException(f"长期记忆配置格式错误: {exc}") from exc
        return config.model_dump(mode="json")

    def _validate_tool_bindings(
        self,
        tools: Sequence[AppToolBindingReq],
        *,
        account_id: UUID,
    ) -> list[dict[str, Any]]:
        """查询工具引用，并剔除当前租户不可见的工具。"""
        valid: list[dict[str, Any]] = []
        for tool in tools:
            tool_type = tool.type
            if tool_type == "builtin_tool":
                exists = self.builtin_provider_manager.get_tool(
                    tool.provider_id,
                    tool.tool_id,
                )
            elif tool_type == "api_tool":
                exists = (
                    self.db.session.query(ApiTool)
                    .filter(
                        ApiTool.provider_id == tool.provider_id,
                        ApiTool.name == tool.tool_id,
                        ApiTool.account_id == account_id,
                    )
                    .one_or_none()
                )
            else:
                server = (
                    self.db.session.query(MCPServer)
                    .filter(
                        MCPServer.id == tool.provider_id,
                        MCPServer.account_id == account_id,
                        MCPServer.enabled.is_(True),
                    )
                    .one_or_none()
                )
                exists = (
                    self.db.session.query(MCPTool)
                    .filter(
                        MCPTool.server_id == getattr(server, "id", None),
                        MCPTool.name == tool.tool_id,
                        MCPTool.account_id == account_id,
                    )
                    .one_or_none()
                    if server
                    else None
                )
            if exists:
                valid.append(tool.model_dump(mode="json"))
        return valid

    def _validate_workflow_bindings(
        self,
        workflows: Sequence[UUID],
        *,
        account_id: UUID,
    ) -> list[str]:
        """保留当前租户可见且已发布的工作流引用。"""
        normalized = [str(workflow_id) for workflow_id in workflows]

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
        datasets: Sequence[UUID],
        *,
        account_id: UUID,
    ) -> list[str]:
        """保留当前租户可见的知识库引用。"""
        normalized = [str(dataset_id) for dataset_id in datasets]

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

    def get_draft_app_config(
        self,
        app: App,
        *,
        request_base_url: str = "",
    ) -> dict[str, Any]:
        """获取应用草稿配置，并清理已失效的关联数据。"""
        # 1.读取应用草稿配置。
        draft_app_config = app.draft_app_config

        normalized_long_term_memory = self._normalize_long_term_memory(
            draft_app_config.long_term_memory
        )
        if draft_app_config.long_term_memory != normalized_long_term_memory:
            self.update(
                draft_app_config,
                long_term_memory=normalized_long_term_memory,
            )

        # 2.校验模型配置；无效的提供方或模型回退到默认值。
        validate_model_config = self._process_and_validate_model_config(
            draft_app_config.model_config,
            account_id=app.account_id,
        )
        if draft_app_config.model_config != validate_model_config:
            self.update(draft_app_config, model_config=validate_model_config)

        # 3.校验工具列表并移除已删除的工具。
        tools, validate_tools = self._process_and_validate_tools(
            draft_app_config.tools,
            account_id=app.account_id,
            request_base_url=request_base_url,
        )

        # 4.有变化时更新草稿配置中的工具列表。
        if draft_app_config.tools != validate_tools:
            # 5.保存校验后的工具列表。
            self.update(draft_app_config, tools=validate_tools)

        # 6.校验知识库列表，移除不存在或已删除的知识库。
        datasets, validate_datasets = self._process_and_validate_datasets(
            draft_app_config.datasets,
            account_id=app.account_id,
        )

        # 7.有变化时更新草稿配置中的知识库列表。
        if set(validate_datasets) != set(draft_app_config.datasets):
            self.update(draft_app_config, datasets=validate_datasets)

        # 8.校验工作流列表。
        workflows, validate_workflows = self._process_and_validate_workflows(
            draft_app_config.workflows,
            account_id=app.account_id,
        )
        if set(validate_workflows) != set(draft_app_config.workflows):
            self.update(draft_app_config, workflows=validate_workflows)
        # 9.转换为接口字典并返回。
        return self._process_and_transformer_app_config(
            validate_model_config,
            tools,
            workflows,
            datasets,
            draft_app_config,
        )

    def get_app_config(self, app: App) -> dict[str, Any]:
        """获取应用运行配置，并清理已失效的关联数据。"""
        # 1.读取应用运行配置。
        app_config = app.app_config

        normalized_long_term_memory = self._normalize_long_term_memory(
            app_config.long_term_memory
        )
        if app_config.long_term_memory != normalized_long_term_memory:
            self.update(
                app_config,
                long_term_memory=normalized_long_term_memory,
            )

        # 发布配置只投影当前能力，不在读取时改写已发布快照。
        validate_model_config = self._process_and_validate_model_config(
            app_config.model_config,
            account_id=app.account_id,
        )

        # 3.校验工具列表并移除已删除的工具。
        tools, validate_tools = self._process_and_validate_tools(
            app_config.tools,
            account_id=app.account_id,
        )

        # 4.有变化时更新运行配置中的工具列表。
        if app_config.tools != validate_tools:
            # 5.保存校验后的工具列表。
            self.update(app_config, tools=validate_tools)

        # 6.校验知识库列表，移除不存在或已删除的知识库。
        app_dataset_joins = app_config.app_dataset_joins
        origin_datasets = [
            str(app_dataset_join.dataset_id) for app_dataset_join in app_dataset_joins
        ]
        datasets, validate_datasets = self._process_and_validate_datasets(
            origin_datasets,
            account_id=app.account_id,
        )

        # 7.删除已失效的知识库关联。
        for dataset_id in set(origin_datasets) - set(validate_datasets):
            with self.db.auto_commit():
                self.db.session.query(AppDatasetJoin).filter(
                    AppDatasetJoin.app_id == app.id,
                    AppDatasetJoin.dataset_id == dataset_id,
                ).delete()

        # 8.校验工作流列表。
        workflows, validate_workflows = self._process_and_validate_workflows(
            app_config.workflows,
            account_id=app.account_id,
        )
        if set(validate_workflows) != set(app_config.workflows):
            self.update(app_config, workflows=validate_workflows)

        # 9.转换为接口字典并返回。
        return self._process_and_transformer_app_config(
            validate_model_config,
            tools,
            workflows,
            datasets,
            app_config,
        )

    def get_agent_runtime_config(self, app: App) -> AgentAppConfigReq:
        """只读加载已发布配置，跳过面向管理页面的重复清理和展示转换。"""
        return self.get_agent_runtime_configs((app,))[app.id]

    def get_agent_runtime_configs(
        self,
        apps: Sequence[App],
    ) -> dict[UUID, AgentAppConfigReq]:
        """批量加载已发布运行配置及其知识库引用。"""
        if not apps:
            return {}
        missing_config_apps = [app for app in apps if app.app_config_id is None]
        if missing_config_apps:
            names = ", ".join(app.name for app in missing_config_apps)
            raise ValidateErrorException(f"应用缺少已发布运行配置: {names}")

        expected_app_by_config = {
            app.app_config_id: app.id
            for app in apps
            if app.app_config_id is not None
        }
        app_ids = {app.id for app in apps}
        rows = (
            self.db.session.query(AppConfig, AppDatasetJoin.dataset_id)
            .outerjoin(
                AppDatasetJoin,
                AppDatasetJoin.app_id == AppConfig.app_id,
            )
            .filter(
                AppConfig.id.in_(expected_app_by_config),
                AppConfig.app_id.in_(app_ids),
            )
            .all()
        )
        configs_by_app: dict[UUID, AppConfig] = {}
        dataset_ids_by_app: dict[UUID, list[UUID]] = {}
        for app_config, dataset_id in rows:
            if expected_app_by_config.get(app_config.id) != app_config.app_id:
                continue
            configs_by_app[app_config.app_id] = app_config
            if dataset_id is not None:
                dataset_ids_by_app.setdefault(app_config.app_id, []).append(dataset_id)

        resolved: dict[UUID, AgentAppConfigReq] = {}
        for app in apps:
            app_config = configs_by_app.get(app.id)
            if app_config is None:
                raise ValidateErrorException(
                    f"应用 {app.name!r} 的已发布运行配置不存在"
                )
            try:
                resolved[app.id] = self._parse_agent_runtime_config(
                    app_config,
                    dataset_ids=dataset_ids_by_app.get(app.id, []),
                )
            except ValidationError as exc:
                raise ValidateErrorException(
                    f"已发布应用 {app.name!r} 的运行配置格式错误: {exc}"
                ) from exc
        return resolved

    @staticmethod
    def _parse_agent_runtime_config(
        app_config: AppConfig,
        *,
        dataset_ids: Sequence[UUID],
    ) -> AgentAppConfigReq:
        """把已发布 ORM 配置转换为 Agent Core 的只读输入。"""
        tool_bindings = AppToolBindingListReq.model_validate(
            app_config.tools or []
        ).root
        return AgentAppConfigReq.model_validate(
            {
                "id": app_config.id,
                "model_config": app_config.model_config,
                "dialog_round": app_config.dialog_round,
                "preset_prompt": app_config.preset_prompt,
                "skills": app_config.skills or [],
                "subagents": app_config.subagents or [],
                "tools": [
                    {
                        "type": binding.type,
                        "provider": {"id": binding.provider_id},
                        "tool": {
                            "id": binding.tool_id,
                            "name": binding.tool_id,
                            "params": dict(binding.params),
                        }
                    }
                    for binding in tool_bindings
                ],
                "workflows": [
                    {"id": workflow_id}
                    for workflow_id in (app_config.workflows or [])
                ],
                "datasets": [{"id": dataset_id} for dataset_id in dataset_ids],
                "retrieval_config": app_config.retrieval_config,
                "long_term_memory": app_config.long_term_memory,
                "opening_statement": app_config.opening_statement,
                "opening_questions": app_config.opening_questions,
                "speech_to_text": app_config.speech_to_text,
                "text_to_speech": app_config.text_to_speech,
                "suggested_after_answer": app_config.suggested_after_answer,
                "review_config": app_config.review_config,
                "updated_at": datetime_to_timestamp(app_config.updated_at),
                "created_at": datetime_to_timestamp(app_config.created_at),
            }
        )

    def get_langchain_tools_by_tools_config(
        self,
        tools_config: list[dict],
        *,
        account_id: UUID,
    ) -> list[BaseTool]:
        """根据工具配置列表构造可调用的 LangChain 工具。"""
        # 1.遍历工具配置列表。
        tools = []
        for tool in tools_config:
            # 2.按工具类型加载对应实例。
            if tool["type"] == "builtin_tool":
                # 3.从内置工具提供方获取工具实例。
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
                except NotFoundException:
                    continue
            else:
                # 4.查询 API 工具记录并创建工具实例。
                api_tool = (
                    self.db.session.query(ApiTool)
                    .filter(
                        ApiTool.provider_id == tool["provider"]["id"],
                        ApiTool.name == tool["tool"]["name"],
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
        """根据工作流 ID 列表构造可调用的 LangChain 工具。"""
        # 1.查询已发布的工作流记录。
        workflow_records = (
            self.db.session.query(Workflow)
            .filter(
                Workflow.id.in_(workflow_ids),
                Workflow.account_id == account_id,
                Workflow.status == WorkflowStatus.PUBLISHED,
            )
            .all()
        )

        # 2.遍历工作流记录。
        workflows = []
        for workflow_record in workflow_records:
            try:
                # 3.创建工作流工具实例。
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
            except (ValidationError, ValidateErrorException):
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
        """把模型、工具、工作流、知识库和应用配置转换为接口字典。"""
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
        request_base_url: str = "",
    ) -> tuple[list[dict], list[dict]]:
        """处理并校验原始工具配置，返回展示数据和有效配置。"""
        # 1.遍历工具列表并移除已删除的工具。
        validate_tools = []
        tools = []
        for tool in origin_tools:
            if tool["type"] == "builtin_tool":
                # 2.查询内置工具提供方并确认其存在。
                provider = self.builtin_provider_manager.get_provider(
                    tool["provider_id"]
                )
                if not provider:
                    continue

                # 3.获取提供方下的工具定义并确认其存在。
                tool_entity = provider.get_tool_entity(tool["tool_id"])
                if not tool_entity:
                    continue

                # 4.校验工具参数；异常参数回退到默认值。
                param_keys = set([param.name for param in tool_entity.params])
                params = tool["params"]
                if set(tool["params"].keys()) - param_keys:
                    params = {
                        param.name: param.default
                        for param in tool_entity.params
                        if param.default is not None
                    }

                # 5.保存校验后的工具配置。
                validate_tools.append({**tool, "params": params})

                # 6.组装内置工具展示信息。
                provider_entity = provider.provider_entity
                tools.append(
                    {
                        "type": "builtin_tool",
                        "provider": {
                            "id": provider_entity.name,
                            "name": provider_entity.name,
                            "label": provider_entity.label,
                            "icon": f"{request_base_url}/builtin-tools/{provider_entity.name}/icon",
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
                # 7.查询 API 工具记录并确认其存在。
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

                # 8.保存校验后的 API 工具配置。
                validate_tools.append(tool)

                # 9.组装 API 工具展示信息。
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
        """处理并校验知识库配置，返回展示数据和有效 ID。"""
        # 1.查询有效知识库并移除不存在或已删除的引用。
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

        # 2.按原始顺序保留有效的知识库 ID。
        validate_datasets = [
            dataset_id for dataset_id in origin_datasets if dataset_id in dataset_sets
        ]

        # 3.组装知识库展示数据。
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
        """处理并校验模型配置，返回当前账号可用的标准结构。"""

        def default_active_model_config() -> dict[str, Any]:
            """返回当前账号可用的默认激活模型配置。"""
            return self.language_model_service.get_default_model_config(account_id)

        # 新建应用时 DEFAULT_APP_CONFIG 中的 model_config 为空对象。
        # 空对象表示“尚未选择模型”，不是一个待解析的 AppModelConfigReq；
        # 这里直接使用当前账号已激活的默认对话模型，并在调用方同步回数据库。
        if origin_model_config == {}:
            return default_active_model_config()

        try:
            parsed = AppModelConfigReq.model_validate(origin_model_config)
        except ValidationError as exc:
            raise ValidateErrorException(f"已保存的模型配置格式错误: {exc}") from exc

        model_config = {
            "provider": parsed.provider,
            "model": parsed.model,
            "provider_connection_id": (
                str(parsed.provider_connection_id)
                if parsed.provider_connection_id is not None
                else None
            ),
            "parameters": dict(parsed.parameters),
        }

        # 停用模型保留原引用供界面修复，不静默切换到其他模型执行。
        try:
            self.language_model_manager.get_provider(model_config["provider"])
        except NotFoundException:
            return dict(origin_model_config)

        try:
            model_entity = self.language_model_service.get_enabled_model_entity(
                model_config["provider"], model_config["model"], account_id,
                provider_connection_id=model_config["provider_connection_id"],
            )
        except ValidateErrorException:
            # 历史无连接 ID 的同名模型仍可打开配置页修复，运行和保存不会绕过校验。
            return dict(origin_model_config)
        if not model_entity:
            return dict(origin_model_config)
        if model_entity.model_type.value != "chat":
            return dict(origin_model_config)

        # 读取时保留历史值供用户修复，不静默改变已发布配置；保存和运行执行严格校验。
        model_config["parameters"] = dict(parsed.parameters)
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

    def _process_and_validate_workflows(
        self,
        origin_workflows: list[UUID],
        *,
        account_id: UUID,
    ) -> tuple[list[dict], list[UUID]]:
        """处理并校验工作流配置，返回展示数据和有效 ID。"""
        # 1.查询已发布工作流并移除无效引用。
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

        # 2.按原始顺序保留有效的工作流 ID。
        validate_workflows = [
            workflow_id
            for workflow_id in origin_workflows
            if workflow_id in workflow_sets
        ]

        # 3.组装工作流展示数据。
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
