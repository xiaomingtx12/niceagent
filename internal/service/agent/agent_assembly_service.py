"""把业务侧 AgentInvocation 转换为一次性的 Agent 执行实例。"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import cast
from uuid import UUID

from injector import inject
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool

from config.config import Config
from internal.core.agent_v2 import (
    AgentConfig,
    AgentExecutor,
    AgentRunContext,
    AgentRuntimeAssembler,
    AgentSkill,
    AsyncSubAgentDefinition,
    SubAgentDefinition,
    SyncSubAgentDefinition,
)
from internal.core.language_model.entities.model_entity import (
    BaseLanguageModel,
)
from internal.core.agent_v2.observability.run_observer import (
    PlatformAgentRunObserver,
)
from internal.entity.subagent_config import (
    SubAgentConfig,
    build_published_graph_id,
)
from internal.service.agent_protocol.internal_auth import (
    build_internal_agent_transport,
)
from internal.entity.app_entity import AppStatus
from internal.entity.conversation_entity import InvokeFrom
from internal.entity.dataset_entity import RetrievalSource
from internal.exception import (
    NotFoundException,
    ValidateErrorException,
)
from internal.extension.logging_extension import logger
from internal.model import Account, App
from internal.repository import WorkspaceFileStore
from internal.repository.agent_workspace_backend import AgentWorkspaceBackend
from internal.core.agent_v2.entity.workspace_quota import AgentWorkspaceLimits
from internal.entity.agent_invocation import (
    AgentInvocation,
)
from internal.schema.agent.agent_attachment_schema import dump_agent_attachments
from internal.schema.app.app_config_schema import AgentAppConfigReq, AppModelConfigReq
from pkg.sqlalchemy import SQLAlchemy
from .agent_attachment_service import AgentAttachmentService
from .agent_model_service import AgentModelService
from .agent_definition_service import AgentDefinitionService
from .agent_workbench_run_service import AgentWorkbenchRunService
from internal.service.app.app_config_service import AppConfigService
from .assistant_agent_service import AssistantAgentService
from internal.service.language_model.language_model_service import LanguageModelService
from internal.service.dataset.retrieval_service import RetrievalService
from internal.service.skill.skill_service import SkillService


@dataclass(frozen=True, slots=True)
class _ResolvedAgentDefinition:
    """一次装配所需的不可变应用定义和已解析能力。"""

    name: str
    description: str
    config: AgentAppConfigReq
    model: BaseLanguageModel
    tools: tuple[BaseTool, ...]
    selected_skills: tuple[str, ...]
    subagents: tuple[SubAgentDefinition, ...]


@inject
@dataclass
class AgentAssemblyService:
    """解析平台应用、授权能力和输入，将资源装配交给 Agent Core。"""

    db: SQLAlchemy
    run_service: AgentWorkbenchRunService
    app_config_service: AppConfigService
    retrieval_service: RetrievalService
    language_model_service: LanguageModelService
    model_service: AgentModelService
    definition_service: AgentDefinitionService
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
            confirmed_user_inputs=self.run_service.get_confirmed_input_context(req),
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


    def _resolve_definition(
        self,
        req: AgentInvocation,
        observer: PlatformAgentRunObserver,
    ) -> _ResolvedAgentDefinition:
        """读取一次应用定义并解析模型、工具和 SubAgent 能力。"""
        # 1.读取草稿或发布配置，并校验本次请求选择的 Skill。
        phase_started_at = time.monotonic()
        definition = self.definition_service.load(req)
        app_config = definition.config
        account_id = definition.account_id
        if req.definition_fingerprint is not None:
            current_fingerprint = definition.fingerprint()
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
                override = self.model_service.resolve_for_context(req, req.model_selection)
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
                model_config, _ = self.model_service.resolve(
                    req, model_config, req.model_selection, account_id=account_id,
                )
            model_config = self.model_service.with_reasoning_override(model_config, req.reasoning_effort)
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
        subagents = self._prepare_subagents(
            app_config.subagents,
            account_id=account_id,
            parent_app_id=req.app_id,
            parent_model=cast(BaseChatModel, model),
            parent_tools=tools,
        )
        self._log_build_timing(req, "subagents", phase_started_at, observer)

        # 5.返回不可变的装配快照，供运行时工厂和图工厂继续使用。
        return _ResolvedAgentDefinition(
            name=definition.name,
            description=definition.description,
            config=app_config,
            model=model,
            tools=tuple(tools),
            selected_skills=selected_skills,
            subagents=tuple(subagents),
        )


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
        parent_tools: Sequence[BaseTool],
    ) -> list[SubAgentDefinition]:
        """平台解析模型来源及工具授权，Core 仅接收本轮可执行的同步或异步定义。"""
        published_apps = self._load_published_apps(subagents, account_id=account_id)
        sync_app_ids = dict.fromkeys(
            config.published_app_id for config in subagents
            if config.source == "published" and config.mode == "sync"
        )
        published_configs = self.app_config_service.get_agent_runtime_configs(
            [published_apps[app_id] for app_id in sync_app_ids]
        )
        available_tools = {tool.name: tool for tool in parent_tools}
        resolved: list[SubAgentDefinition] = []
        for config in subagents:
            if config.mode == "async":
                resolved.append(self._prepare_async_subagent(
                    config, account_id=account_id, parent_app_id=parent_app_id,
                ))
            elif config.source == "published":
                target = published_apps[config.published_app_id]
                resolved.append(self._prepare_sync_published_subagent(
                    config, target, published_configs[target.id], account_id=account_id,
                ))
            else:
                resolved.append(self._prepare_sync_custom_subagent(
                    config, account_id=account_id,
                    parent_model=parent_model, available_tools=available_tools,
                ))
        return resolved

    def _prepare_sync_custom_subagent(
        self,
        config: SubAgentConfig,
        *,
        account_id: UUID,
        parent_model: BaseChatModel,
        available_tools: Mapping[str, BaseTool],
    ) -> SyncSubAgentDefinition:
        """跟随模式直接绑定本轮父模型；工具仅从父应用已授权能力中选择。"""
        missing = sorted(set(config.tools) - set(available_tools))
        if missing:
            raise ValidateErrorException(
                f"subagent {config.name!r} 引用了未授权工具: {', '.join(missing)}"
            )
        model = self._load_subagent_model(config, account_id=account_id)
        return SyncSubAgentDefinition(
            name=config.name,
            description=config.description,
            system_prompt=config.system_prompt,
            model=parent_model if model is None else model,
            tools=tuple(available_tools[name] for name in config.tools),
            skills=tuple(config.skills),
        )

    def _prepare_sync_published_subagent(
        self,
        config: SubAgentConfig,
        target: App,
        target_config: AgentAppConfigReq,
        *,
        account_id: UUID,
    ) -> SyncSubAgentDefinition:
        """同步发布引用使用目标应用的发布模型和能力，不继承父应用模型。"""
        if target_config.model_config_data is None:
            raise ValidateErrorException(f"发布 Agent {target.name!r} 缺少模型配置")
        tools = self._build_configured_tools(
            target_config, account_id=account_id, app_id=target.id,
        )
        model = self.language_model_service.load_language_model(
            target_config.model_config_data, account_id=account_id,
        )
        return SyncSubAgentDefinition(
            name=config.name,
            description=config.description,
            system_prompt=(
                f"你是已发布应用「{target.name}」的执行 Agent。"
                "请严格遵循该应用的发布人设完成任务，并只返回本次任务的最终结果。\n\n"
                f"{target_config.preset_prompt}"
            ).strip(),
            tools=tuple(tools),
            model=cast(BaseChatModel, model),
        )

    def _prepare_async_subagent(
        self,
        config: SubAgentConfig,
        *,
        account_id: UUID,
        parent_app_id: UUID,
    ) -> AsyncSubAgentDefinition:
        """发布引用完成内部认证绑定，外部目标沿用已校验的 Protocol 配置。"""
        graph_id, url, headers = config.graph_id, config.url, dict(config.headers)
        if config.source == "published":
            target_id = config.published_app_id
            if target_id is None:
                raise ValidateErrorException(f"子 Agent {config.name!r} 缺少发布应用标识")
            transport = build_internal_agent_transport(
                settings=self.config,
                account_id=account_id, parent_app_id=parent_app_id,
                target_app_id=target_id, headers=headers,
            )
            graph_id = build_published_graph_id(target_id)
            url = cast(str, transport["url"])
            headers = cast(dict[str, str], transport["headers"])
        if not graph_id:
            raise ValidateErrorException(f"子 Agent {config.name!r} 缺少 graph_id")
        return AsyncSubAgentDefinition(
            name=config.name, description=config.description,
            graph_id=graph_id, url=url, headers=headers,
        )

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
