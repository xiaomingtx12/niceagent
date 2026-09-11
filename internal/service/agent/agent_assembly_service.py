"""把业务侧 AgentInvocation 转换为一次性的 Agent 执行实例。"""

from __future__ import annotations

import time
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import cast
from uuid import UUID

from injector import inject
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool
from pydantic import ValidationError

from config.config import Config
from internal.core.agent_v2 import (
    AgentConfig,
    AgentExecutor,
    AgentRunContext,
    AgentRuntimeAssembler,
    AgentSkill,
    SubAgentRuntimeBinding,
)
from internal.core.language_model.entities.model_entity import (
    BaseLanguageModel, ModelEntity, ModelParameter, ModelType,
)
from internal.core.language_model.parameters import canonicalize_parameters
from internal.core.agent_v2.observability.run_observer import (
    PlatformAgentRunObserver,
)
from internal.core.agent_v2.entity.subagent_config import (
    SubAgentConfig,
    build_published_graph_id,
)
from internal.service.agent_protocol.internal_auth import (
    issue_internal_agent_protocol_token,
)
from internal.entity.app_entity import AppStatus
from internal.entity.conversation_entity import InvokeFrom
from internal.entity.dataset_entity import RetrievalSource
from internal.exception import (
    NotFoundException,
    ValidateErrorException,
)
from internal.extension.logging_extension import logger
from internal.model import Account, App, AgentRun
from internal.core.agent_v2.entity.user_input import build_confirmed_input_context
from internal.repository import WorkspaceFileStore
from internal.repository.agent_workspace_backend import AgentWorkspaceBackend
from internal.core.agent_v2.entity.workspace_quota import AgentWorkspaceLimits
from internal.schema.agent.agent_runtime_schema import (
    AgentInvocation, AgentInvocationContext,
    WorkbenchModelSettingsResp, WorkbenchModelChoiceResp,
)
from internal.entity.agent_workbench_entity import WorkbenchModelSelection
from internal.schema.agent.agent_attachment_schema import dump_agent_attachments
from internal.schema.app.app_config_schema import AgentAppConfigReq, AppModelConfigReq
from pkg.sqlalchemy import SQLAlchemy
from .agent_attachment_service import AgentAttachmentService
from internal.service.app.app_config_service import AppConfigService
from .assistant_agent_service import AssistantAgentService
from internal.service.language_model.language_model_service import LanguageModelService
from internal.service.dataset.retrieval_service import RetrievalService
from internal.service.skill.skill_service import SkillService


_MODEL_SELECTABLE_SOURCES = frozenset({
    InvokeFrom.DEBUGGER, InvokeFrom.CREATOR_TASK, InvokeFrom.ASSISTANT_AGENT,
})


@dataclass(frozen=True, slots=True)
class _ResolvedAgentDefinition:
    """一次装配所需的不可变应用定义和已解析能力。"""

    name: str
    description: str
    config: AgentAppConfigReq
    model: BaseLanguageModel
    tools: tuple[BaseTool, ...]
    selected_skills: tuple[str, ...]
    subagents: tuple[SubAgentConfig, ...]
    subagent_bindings: Mapping[str, SubAgentRuntimeBinding]


@inject
@dataclass
class AgentAssemblyService:
    """解析平台应用、授权能力和输入，将资源装配交给 Agent Core。"""

    db: SQLAlchemy
    app_config_service: AppConfigService
    retrieval_service: RetrievalService
    language_model_service: LanguageModelService
    agent_attachment_service: AgentAttachmentService
    assistant_agent_service: AssistantAgentService
    skill_service: SkillService
    workspace_file_store: WorkspaceFileStore
    config: Config


    def build_agent(
        self,
        req: AgentInvocation,
        *,
        observer: PlatformAgentRunObserver,
    ) -> tuple[AgentExecutor, BaseMessage]:
        """准备平台能力并委托 Core 装配，返回仅属于当前 Run 的执行器和输入。"""
        # 1.读取平台配置并转换为 Agent 输入消息。
        build_started_at = time.monotonic()
        definition = self._resolve_definition(req, observer)
        input_message = definition.model.convert_to_human_message(
            req.query,
            req.image_urls,
            dump_agent_attachments(req.attachments),
        )
        if req.input_message_id:
            input_message.id = req.input_message_id
        input_message.additional_kwargs.update(
            {
                "business_message_id": str(req.message_id),
                "run_id": str(observer.run_id),
            }
        )

        # 2.平台只负责解析已授权 Skill，Agent Core 接管资源和图生命周期。
        context = AgentRunContext(
            account_id=req.account_id,
            app_id=req.app_id,
            conversation_id=req.conversation_id,
            created_by=req.created_by,
            principal_id=(req.end_user_id or req.created_by),
            memory_enabled=definition.config.long_term_memory.enable,
            planning_enabled=req.planning_enabled,
            user_input_enabled=req.user_input_enabled and definition.config.user_input.enable,
            confirmed_user_inputs=self._confirmed_input_context(req),
        )
        skills = self.skill_service.resolve_enabled_skills(definition.config.skills)
        agent_config = AgentConfig(
            name=definition.name,
            description=definition.description,
            model=cast(BaseChatModel, definition.model),
            preset_prompt=definition.config.preset_prompt,
            review_config=definition.config.review_config.model_dump(mode="json"),
            tools=definition.tools,
            skills=tuple(
                AgentSkill(
                    name=skill.name,
                    skill_md=skill.skill_md,
                    package_root=skill.package_root,
                    package_files=dict(skill.package_files),
                )
                for skill in skills
            ),
            selected_skills=definition.selected_skills,
            subagents=definition.subagents,
            subagent_bindings=definition.subagent_bindings,
        )

        # 3.Core 装配运行时并编译图，平台只提供附件写入操作和异常转换。
        try:
            agent = AgentRuntimeAssembler(
                workspace_factory=partial(
                    AgentWorkspaceBackend.create,
                    engine=self.db.engine,
                    file_store=self.workspace_file_store,
                    limits=AgentWorkspaceLimits(
                        max_bytes=self.config.WORKSPACE_MAX_BYTES,
                        max_files=self.config.WORKSPACE_MAX_FILES,
                    ),
                ),
                config=self.config,
            ).build(
                context=context,
                definition=agent_config,
                observer=observer,
                prepare_backend=partial(
                    self.agent_attachment_service.materialize,
                    req.attachments,
                ),
            )
        except ValueError as exc:
            raise ValidateErrorException(f"Agent 运行配置无效: {exc}") from exc
        self._log_build_timing(req, "total", build_started_at, observer)
        return agent, input_message

    def get_definition_fingerprint(self, req: AgentInvocation) -> str:
        """绑定暂停前的应用配置，避免恢复时使用不同的图装配定义。"""
        name, description, _, config = self._load_app_configuration(req)
        return self._definition_fingerprint(name, description, config)

    def resolve_workbench_model(
        self,
        context: AgentInvocationContext,
        selection: WorkbenchModelSelection | None = None,
    ) -> AppModelConfigReq | None:
        """创建者可覆盖自己的已激活模型；公开入口不能借此选择账号中的其他连接。"""
        config, _ = self._resolve_workbench_model(context, selection)
        return config

    def get_workbench_model_settings(
        self,
        context: AgentInvocationContext,
        selection: WorkbenchModelSelection | None = None,
    ) -> WorkbenchModelSettingsResp:
        """按已授权入口读取模型设置，公开入口不暴露账号的可选连接目录。"""
        # 1.复用覆盖校验已经取得的模型；没有模型时返回空设置。
        config, model = self._resolve_workbench_model(context, selection)
        account_id = context.account_id
        if config is None:
            return WorkbenchModelSettingsResp()
        if model is None:
            model = self._get_model_entity(config, account_id)
        if model is None:
            return WorkbenchModelSettingsResp()

        # 2.思考强度优先采用应用配置，未设置时才采用模型声明的默认值。
        parameter = self._reasoning_parameter(model)
        parameters = canonicalize_parameters(config.parameters)
        default = parameters.get("reasoning_effort")
        reasoning_levels = []
        if parameter is not None:
            reasoning_levels = [option.value for option in parameter.options]
            if default is None:
                default = parameter.default

        # 3.没有显式选择时，仅为允许切换模型的创建者补全当前连接。
        selected_model = selection
        can_select_model = context.invoke_from in _MODEL_SELECTABLE_SOURCES and context.created_by == account_id
        connection_id = config.provider_connection_id or model.attributes.get("provider_api_key_id")
        needs_default_selection = selected_model is None and can_select_model
        if needs_default_selection and connection_id:
            selected_model = WorkbenchModelSelection(
                provider=config.provider,
                model=config.model,
                provider_connection_id=connection_id,
            )

        # 4.构造响应时只引用已算好的字段，不在响应内部混入权限和回退分支。
        return WorkbenchModelSettingsResp(
            model=config.model,
            provider=config.provider,
            selected_model=selected_model,
            model_override=selection,
            available_models=self._workbench_model_choices(context),
            reasoning_levels=reasoning_levels,
            default_reasoning_effort=default,
        )

    def validate_workbench_reasoning(
        self,
        context: AgentInvocationContext,
        effort: str | None,
        selection: WorkbenchModelSelection | None = None,
    ) -> None:
        """任务覆盖只允许当前模型明确声明的等级，在创建消息前拒绝无效选择。"""
        # 1.完全没有覆盖时不查模型；只切换模型时仍需校验连接权限。
        if selection is None and effort is None:
            return
        config, model = self._resolve_workbench_model(context, selection)
        if effort is None:
            return
        if model is None and config is not None:
            model = self._get_model_entity(config, context.account_id)

        # 2.必须在该模型声明的可选强度内，不能仅凭参数名称接受客户端输入。
        parameter = self._reasoning_parameter(model)
        if parameter is None:
            raise ValidateErrorException("当前模型不支持所选思考强度，请重新选择")
        allowed_efforts = {option.value for option in parameter.options}
        if effort not in allowed_efforts:
            raise ValidateErrorException("当前模型不支持所选思考强度，请重新选择")

    def _confirmed_input_context(self, req: AgentInvocation) -> str:
        """只加载同一授权会话入口的近期答案，不从客户端接收或写入长期记忆。"""
        rows = (
            self.db.session.query(AgentRun.state_snapshot["input_history"])
            .filter(AgentRun.conversation_id == req.conversation_id, AgentRun.app_id == req.app_id,
                    AgentRun.invoke_from == req.invoke_from.value,
                    AgentRun.state_snapshot["input_history"].astext.isnot(None))
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(8).all()
        )
        return build_confirmed_input_context([history for (history,) in rows if isinstance(history, list)])

    def _resolve_definition(
        self,
        req: AgentInvocation,
        observer: PlatformAgentRunObserver,
    ) -> _ResolvedAgentDefinition:
        """读取一次应用定义并解析模型、工具和 SubAgent 能力。"""
        # 1.读取草稿或发布配置，并校验本次请求选择的 Skill。
        phase_started_at = time.monotonic()
        app_name, app_description, account_id, app_config = (
            self._load_app_configuration(req)
        )
        if req.definition_fingerprint is not None:
            current_fingerprint = self._definition_fingerprint(app_name, app_description, app_config)
            if req.definition_fingerprint != current_fingerprint:
                raise ValidateErrorException("应用配置已变更，请恢复原配置后继续填写，或创建新会话")
        selected_skills = self._validate_selected_skills(
            app_config,
            req.selected_skills,
        )
        self._log_build_timing(req, "config", phase_started_at, observer)

        # 2.按调用来源解析主模型；Assistant Agent 使用内置能力集合。
        phase_started_at = time.monotonic()
        if req.invoke_from is InvokeFrom.ASSISTANT_AGENT:
            account = self.db.session.get(Account, req.account_id)
            if account is None:
                raise NotFoundException("账号不存在")
            model_override = None
            if req.model_selection:
                override = self.resolve_workbench_model(req, req.model_selection)
                if override is not None:
                    model_override = override.model_dump(mode="json")
            model, tools = self.assistant_agent_service.build_capabilities(
                account, reasoning_effort=req.reasoning_effort,
                model_config_override=model_override,
            )
        else:
            if app_config.model_config_data is None:
                raise ValidateErrorException("Agent 运行配置缺少模型")
            model_config = app_config.model_config_data
            if req.model_selection:
                model_config = self.resolve_workbench_model(req, req.model_selection)
            model_config = self._with_reasoning_override(model_config, req.reasoning_effort)
            model = self.language_model_service.load_language_model(
                model_config, account_id=account_id,
            )
            tools = []
        self._log_build_timing(req, "model", phase_started_at, observer)

        # 3.把应用配置中的普通工具、知识库和工作流转换为运行时工具。
        phase_started_at = time.monotonic()
        if req.invoke_from is not InvokeFrom.ASSISTANT_AGENT:
            tools = self._build_configured_tools(
                app_config,
                account_id=account_id,
                app_id=req.app_id,
            )
        self._log_build_timing(req, "tools", phase_started_at, observer)

        # 4.解析同步和异步 SubAgent 的运行时绑定。
        phase_started_at = time.monotonic()
        subagents, bindings = self._prepare_subagents(
            app_config.subagents,
            account_id=account_id,
            parent_app_id=req.app_id,
            parent_model=cast(BaseChatModel, model),
        )
        self._log_build_timing(req, "subagents", phase_started_at, observer)

        # 5.返回不可变的装配快照，供运行时工厂和图工厂继续使用。
        return _ResolvedAgentDefinition(
            name=app_name,
            description=app_description,
            config=app_config,
            model=model,
            tools=tuple(tools),
            selected_skills=selected_skills,
            subagents=tuple(subagents),
            subagent_bindings=bindings,
        )

    def _resolve_workbench_model(
        self,
        context: AgentInvocationContext,
        selection: WorkbenchModelSelection | None,
    ) -> tuple[AppModelConfigReq | None, ModelEntity | None]:
        """保留应用默认参数；只有切换到其他模型或连接时才采用新模型默认值。"""
        # 1.公开入口不能借用账号的其他模型连接。
        if selection is not None and context.invoke_from not in _MODEL_SELECTABLE_SOURCES:
            raise ValidateErrorException("当前入口不允许切换模型")

        # 2.辅助 Agent 读取账号默认模型，其他入口读取草稿或发布配置。
        if context.invoke_from is InvokeFrom.ASSISTANT_AGENT:
            default_config = self.language_model_service.get_default_model_config(context.account_id)
            config = AppModelConfigReq.model_validate(default_config)
            account_id = context.account_id
        else:
            _, _, account_id, app_config = self._load_app_configuration(context)
            config = app_config.model_config_data
        if selection is None:
            return config, None

        # 3.模型覆盖必须同时满足应用归属和调用者身份，且连接仍然启用。
        if account_id != context.account_id or context.created_by != account_id:
            raise ValidateErrorException("只能选择当前账号的模型")
        model = self.language_model_service.get_enabled_model_entity(
            selection.provider, selection.model, account_id,
            provider_connection_id=str(selection.provider_connection_id),
        )
        if model is None or model.model_type != ModelType.CHAT:
            raise ValidateErrorException("所选模型未激活或不是对话模型")

        # 4.旧配置未记录连接 ID 时沿用原参数；明确换连接则重建默认参数。
        same_model = config is not None and (
            config.provider == selection.provider and config.model == selection.model
        )
        same_connection = config is not None and (
            config.provider_connection_id is None
            or config.provider_connection_id == selection.provider_connection_id
        )
        if same_model and same_connection:
            return config, model
        parameters = {
            parameter.name: parameter.default for parameter in model.parameters
            if parameter.required and parameter.default is not None
        }
        selected_config = AppModelConfigReq(**selection.model_dump(), parameters=parameters)
        return selected_config, model

    def _workbench_model_choices(self, context: AgentInvocationContext) -> list[WorkbenchModelChoiceResp]:
        """目录按可信账号和创建者身份过滤，不向 WebApp 或开放 API 暴露。"""
        if context.invoke_from not in _MODEL_SELECTABLE_SOURCES:
            return []
        if context.created_by != context.account_id:
            return []

        # 1.按账号一次读取模型目录，每个连接保留自己的模型与参数选项。
        choices = []
        groups = self.language_model_service.get_language_models(context.account_id, {ModelType.CHAT})
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
    def _with_reasoning_override(config: AppModelConfigReq, effort: str | None) -> AppModelConfigReq:
        """复制调用参数覆盖单个字段，避免改写草稿、发布配置及旧字段别名冲突。"""
        if effort is None:
            return config
        parameters = {key: value for key, value in config.parameters.items()
                      if key not in {"reasoning_effort", "thinking_level", "thinking"}}
        return config.model_copy(update={"parameters": {**parameters, "reasoning_effort": effort}})

    @staticmethod
    def _definition_fingerprint(name: str, description: str, config: AgentAppConfigReq) -> str:
        """指纹比较规范化后的模型字段，新增模式标记不使未变更的旧待填任务失效。"""
        payload = config.model_dump(
            mode="json", exclude={"subagents": {"__all__": {"model_mode"}}},
        )
        value = json.dumps([name, description, payload], sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(value.encode()).hexdigest()

    def _load_app_configuration(
        self,
        req: AgentInvocationContext,
    ) -> tuple[str, str, UUID, AgentAppConfigReq]:
        """在装配时读取唯一应用配置；内置辅助 Agent 使用代码内定义。"""
        # 1.Assistant Agent 不依赖 App 配置，直接返回内置定义。
        if req.invoke_from is InvokeFrom.ASSISTANT_AGENT:
            return (
                "应用构建助手",
                "帮助 Creator 创建应用和工作流。",
                req.account_id,
                AgentAppConfigReq(),
            )

        # 2.读取并校验当前租户可访问的 App。
        app = self.db.session.get(App, req.app_id)
        if app is None:
            raise NotFoundException("应用不存在")
        try:
            # 3.Debugger 使用草稿，其余入口使用可执行的发布配置。
            if req.invoke_from is InvokeFrom.DEBUGGER:
                app_config = self.app_config_service.get_draft_agent_runtime_config(app)
            else:
                app_config = self.app_config_service.get_agent_runtime_config(app)
        except (ValidationError, ValidateErrorException) as exc:
            raise ValidateErrorException(f"应用运行配置无效: {exc}") from exc
        return app.name, app.description, app.account_id, app_config

    def _build_configured_tools(
        self,
        app_config: AgentAppConfigReq,
        *,
        account_id: UUID,
        app_id: UUID,
    ) -> list[BaseTool]:
        """把应用的工具、知识库和工作流配置转换为运行时工具。"""
        # 1.加载 App 配置中声明的普通工具。
        tools = self.app_config_service.get_langchain_tools_by_tools_config(
            app_config.dump_tools(),
            account_id=account_id,
        )

        # 2.把知识库配置转换为检索工具。
        if app_config.datasets:
            tools.append(
                self.retrieval_service.create_langchain_tool_from_search(
                    dataset_ids=[dataset.id for dataset in app_config.datasets],
                    account_id=account_id,
                    retrival_source=RetrievalSource.APP,
                    source_app_id=app_id,
                    **app_config.retrieval_config.model_dump(mode="python"),
                )
            )

        # 3.把工作流配置转换为可调用工具。
        if app_config.workflows:
            tools.extend(
                self.app_config_service.get_langchain_tools_by_workflow_ids(
                    [workflow.id for workflow in app_config.workflows],
                    account_id=account_id,
                )
            )

        # 4.返回当前 Agent 已授权的工具集合。
        return tools

    def _prepare_subagents(
        self,
        subagents: Sequence[SubAgentConfig],
        *,
        account_id: UUID,
        parent_app_id: UUID,
        parent_model: BaseChatModel,
    ) -> tuple[list[SubAgentConfig], dict[str, SubAgentRuntimeBinding]]:
        """每次运行重新绑定模型；跟随子 Agent 共享本次解析后的父模型实例。"""
        # 1.发布应用统一授权，同步引用的配置批量加载。
        published_apps = self._load_published_apps(subagents, account_id=account_id)
        sync_app_ids = dict.fromkeys(
            config.published_app_id for config in subagents
            if config.source == "published" and config.mode == "sync"
        )
        published_configs = self.app_config_service.get_agent_runtime_configs(
            [published_apps[app_id] for app_id in sync_app_ids]
        )
        normalized: list[SubAgentConfig] = []
        bindings: dict[str, SubAgentRuntimeBinding] = {}

        # 2.外部异步、发布引用和本地自定义分别解析，不在一个分支里混用模型来源。
        for config in subagents:
            if config.source == "published":
                target = published_apps[config.published_app_id]
                if config.mode == "async":
                    normalized.append(self._prepare_async_published_subagent(
                        config, account_id=account_id, parent_app_id=parent_app_id,
                    ))
                    continue
                resolved, binding = self._prepare_sync_published_subagent(
                    config, target, published_configs[target.id], account_id=account_id,
                )
                normalized.append(resolved)
                bindings[config.name] = binding
                continue

            normalized.append(config.model_copy(deep=True))
            if config.mode == "sync":
                model = self._load_subagent_model(config, account_id=account_id)
                bindings[config.name] = SubAgentRuntimeBinding(
                    model=parent_model if model is None else model,
                )
        return normalized, bindings

    def _prepare_sync_published_subagent(
        self,
        config: SubAgentConfig,
        target: App,
        target_config: AgentAppConfigReq,
        *,
        account_id: UUID,
    ) -> tuple[SubAgentConfig, SubAgentRuntimeBinding]:
        """同步发布引用使用目标应用的发布模型和能力，不继承父应用模型。"""
        if target_config.model_config_data is None:
            raise ValidateErrorException(f"发布 Agent {target.name!r} 缺少模型配置")
        tools = self._build_configured_tools(
            target_config, account_id=account_id, app_id=target.id,
        )
        model = self.language_model_service.load_language_model(
            target_config.model_config_data, account_id=account_id,
        )
        resolved = config.model_copy(deep=True, update={
            "system_prompt": (
                f"你是已发布应用「{target.name}」的执行 Agent。"
                "请严格遵循该应用的发布人设完成任务，并只返回本次任务的最终结果。\n\n"
                f"{target_config.preset_prompt}"
            ).strip(),
            "tools": [tool.name for tool in tools],
            "skills": [],
        })
        return resolved, SubAgentRuntimeBinding(
            tools=tuple(tools), model=cast(BaseChatModel, model),
        )

    def _prepare_async_published_subagent(
        self,
        config: SubAgentConfig,
        *,
        account_id: UUID,
        parent_app_id: UUID,
    ) -> SubAgentConfig:
        """异步发布引用只绑定内部传输，模型由目标应用在独立运行中解析。"""
        target_id = config.published_app_id
        if target_id is None:
            raise ValidateErrorException(f"子 Agent {config.name!r} 缺少发布应用标识")
        transport = self._build_internal_async_transport(
            account_id=account_id, parent_app_id=parent_app_id,
            target_app_id=target_id, headers=config.headers,
        )
        return config.model_copy(deep=True, update={
            "graph_id": build_published_graph_id(target_id), **transport,
        })

    @staticmethod
    def _validate_selected_skills(
        app_config: AgentAppConfigReq,
        selected_skills: Sequence[str],
    ) -> tuple[str, ...]:
        """确认客户端选择的 Skill 已在当前运行配置中挂载并启用。"""
        # 1.没有客户端选择时，交给运行时使用全部已启用 Skill。
        if not selected_skills:
            return ()

        # 2.只允许选择当前配置中已挂载且启用的 Skill。
        enabled_names = {
            binding.name for binding in app_config.skills if binding.enabled
        }
        missing = sorted(set(selected_skills) - enabled_names)
        if missing:
            raise ValidateErrorException(
                f"所选 Skill 未挂载或未启用: {', '.join(missing)}"
            )

        # 3.返回稳定的不可变名称集合。
        return tuple(selected_skills)

    def _load_published_apps(
        self,
        subagents: Sequence[SubAgentConfig],
        *,
        account_id: UUID,
    ) -> dict[UUID, App]:
        """批量读取当前租户仍处于发布状态的 SubAgent 应用。"""
        # 1.收集去重后的发布应用 ID。
        target_ids = {}
        for config in subagents:
            if config.source != "published" or config.published_app_id is None:
                continue
            target_ids[config.published_app_id] = None
        if not target_ids:
            return {}

        # 2.按租户和发布状态批量查询目标应用。
        apps = (
            self.db.session.query(App)
            .filter(
                App.id.in_(target_ids),
                App.account_id == account_id,
                App.status == AppStatus.PUBLISHED,
            )
            .all()
        )

        # 3.拒绝不存在、越权或已经取消发布的引用。
        apps_by_id = {app.id: app for app in apps}
        missing = [target_id for target_id in target_ids if target_id not in apps_by_id]
        if missing:
            raise NotFoundException(
                "子 Agent 引用的发布应用不存在或已取消发布: "
                + ", ".join(str(target_id) for target_id in missing)
            )

        # 4.返回按应用 ID 索引的查询结果。
        return apps_by_id

    def _build_internal_async_transport(
        self,
        *,
        account_id: UUID,
        parent_app_id: UUID,
        target_app_id: UUID,
        headers: dict[str, str],
    ) -> dict[str, object]:
        """为平台内部发布 Agent 绑定 URL 和不可伪造的作用域凭证。"""
        # 1.为父子 Agent 作用域签发短期内部协议 Token。
        base_url = self.config.AGENT_INTERNAL_PROTOCOL_BASE_URL.rstrip("/")
        secret = self.config.AGENT_INTERNAL_PROTOCOL_SECRET
        ttl_seconds = self.config.AGENT_INTERNAL_PROTOCOL_TOKEN_TTL_SECONDS
        token = issue_internal_agent_protocol_token(
            account_id=account_id,
            parent_app_id=parent_app_id,
            target_app_id=target_app_id,
            secret=secret,
            ttl_seconds=ttl_seconds,
        )

        # 2.移除配置中可能伪造的内部 Token Header。
        # 禁止持久化配置伪造内部凭证；大小写不同的 HTTP Header 也必须移除，
        # 否则 HTTP 客户端可能发送重复 Header，导致服务端解析结果不确定。
        safe_headers = {
            key: value
            for key, value in headers.items()
            if key.lower() != "x-mydify-internal-token"
        }

        # 3.返回异步 SubAgent 所需的内部传输参数。
        return {
            "url": base_url,
            "headers": {
                **safe_headers,
                "X-MyDify-Internal-Token": token,
            },
        }

    def _load_subagent_model(
        self,
        subagent: SubAgentConfig,
        *,
        account_id: UUID,
    ) -> BaseChatModel | None:
        """解析自定义 SubAgent 的可选模型覆盖。"""
        # 1.跟随模式不加载独立模型，由本次装配绑定父模型。
        if subagent.model_mode == "inherit" or subagent.model is None:
            return None

        # 2.使用领域层规范化后的模型与连接，租户授权由模型服务再次校验。
        return cast(
            BaseChatModel,
            self.language_model_service.load_language_model(
                AppModelConfigReq(
                    provider=cast(str, subagent.model_provider),
                    model=subagent.model,
                    provider_connection_id=subagent.model_connection_id,
                    parameters={},
                ),
                account_id=account_id,
            ),
        )

    @staticmethod
    def _log_build_timing(
        req: AgentInvocation,
        stage: str,
        started_at: float,
        observer: PlatformAgentRunObserver,
    ) -> None:
        """记录 Agent 装配子阶段耗时。"""
        # 1.计算当前装配阶段耗时。
        duration_ms = (time.monotonic() - started_at) * 1000

        # 2.写入平台观测数据。
        observer.record_stage(f"assembly.{stage}", duration_ms)

        # 3.输出可按应用和会话检索的结构化日志。
        logger.info(
            "agent_assembly_timing app_id=%s conversation_id=%s "
            "stage=%s duration_ms=%.2f",
            req.app_id,
            req.conversation_id,
            stage,
            duration_ms,
        )


__all__ = ["AgentAssemblyService"]
