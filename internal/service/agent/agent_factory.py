"""按 AgentInvocation 装配一次性的 Agent 执行实例。"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from injector import inject
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.tools import BaseTool
from sqlalchemy.orm import Session

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
    PlatformAgentRunObserver,
    AgentWorkspaceLimits,
)
from internal.core.language_model.entities.model_entity import (
    BaseLanguageModel,
)
from internal.entity.subagent_config import (
    SubAgentConfig,
    build_published_graph_id,
)
from internal.service.internal_agent_protocol.auth import (
    build_internal_agent_transport,
)
from internal.entity.app_entity import AppStatus
from internal.entity.conversation_entity import InvokeFrom
from internal.entity.dataset_entity import RetrievalSource
from internal.exception import (
    FailException,
    NotFoundException,
    ValidateErrorException,
)
from internal.extension.logging_extension import logger
from internal.model import Account, App
from internal.repository import WorkspaceFileRepository, WorkspaceFileStore
from internal.repository.agent_workspace_store import AgentWorkspaceStore
from internal.core.agent_v2.persistence.workspace_store import WorkspaceDeliveryError
from internal.entity.agent_invocation import (
    AgentInvocation,
)
from internal.schema.agent.agent_attachment_schema import dump_agent_attachments
from internal.schema.app.app_config_schema import AgentAppConfigReq, AppModelConfigReq
from pkg.sqlalchemy import SQLAlchemy
from .agent_attachment_service import AgentAttachmentService
from .agent_model_service import AgentModelService
from .agent_definition_service import AgentDefinitionService
from internal.service.app.app_config_service import AppConfigService
from .assistant_agent_service import AssistantAgentService
from internal.service.language_model.language_model_service import LanguageModelService
from internal.service.dataset.retrieval_service import RetrievalService
from internal.service.skill.skill_service import SkillService


@inject
@dataclass
class AgentFactory:
    """解析平台应用、授权能力和输入，将资源装配交给 Agent Core。"""

    db: SQLAlchemy
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

    def create_executor(
        self,
        req: AgentInvocation,
        *,
        observer: PlatformAgentRunObserver,
    ) -> tuple[AgentExecutor, BaseMessage]:
        """准备平台能力并委托 Core 装配，返回仅属于当前 Run 的执行器和输入。"""
        # 1.读取平台配置并转换为 Agent 输入消息。
        build_started_at = time.monotonic()
        agent_config, context = self._resolve_agent_config(req)
        input_message = cast(BaseLanguageModel, agent_config.model).convert_to_human_message(
            req.query,
            req.image_urls,
            dump_agent_attachments(req.attachments),
        )
        if req.input_message_id:
            input_message.id = req.input_message_id
        input_message.additional_kwargs.update(
            {
                "business_message_id": str(req.turn_id),
                "run_id": str(observer.run_id),
            }
        )

        # 2.平台准备附件数据和授权存储对象，框架 Store、Checkpoint 和 Backend 由 Core 装配。
        try:
            input_files = self.agent_attachment_service.load_files(req.attachments)
            workspace_store = self._build_workspace_store(
                context,
            )
            # build 从入口接管本轮存储；不再把资源工厂和附件写入回调传入 Core。
            agent = AgentRuntimeAssembler(
                config=self.config,
                workspace_store=workspace_store,
            ).build(
                context=context,
                definition=agent_config,
                observer=observer,
                input_files=input_files,
            )
        except WorkspaceDeliveryError as exc:
            raise FailException(str(exc)) from exc
        except ValueError as exc:
            raise ValidateErrorException(f"Agent 运行配置无效: {exc}") from exc
        self._log_build_timing(req, "total", build_started_at)
        return agent, input_message


    def _build_workspace_store(
        self,
        context: AgentRunContext,
    ) -> AgentWorkspaceStore:
        """绑定本轮文件交付存储；仅配置仓储，不查询文件或读取 COS。"""
        # 1.独立于 HTTP 请求会话，避免后台执行受请求结束影响。
        session = Session(bind=self.db.engine, expire_on_commit=False)
        try:
            # 2.在平台边界绑定授权作用域和配额，事务交由仓储管理。
            repository = WorkspaceFileRepository(
                session=session,
                account_id=context.account_id,
                app_id=context.app_id,
                scope_id=context.workspace_scope_id,
                created_by=context.created_by,
                limits=AgentWorkspaceLimits(
                    max_bytes=self.config.WORKSPACE_MAX_BYTES,
                    max_files=self.config.WORKSPACE_MAX_FILES,
                ),
                owns_session=True,
            )
            # 3.存储适配器只服务按需加载和显式交付，运行资源负责释放会话。
            return AgentWorkspaceStore(
                repository=repository,
                file_store=self.workspace_file_store,
            )
        except BaseException:
            try:
                session.close()
            except Exception:
                logger.exception("Workspace Store 装配失败后的 Session 清理失败")
            raise

    def _resolve_agent_config(
        self,
        req: AgentInvocation,
    ) -> tuple[AgentConfig, AgentRunContext]:
        """一次配置读取直接生成 Core 所需定义与可信上下文，不再构造中间能力快照。"""
        # 1.读取草稿或发布配置，并校验本次请求选择的 Skill。
        phase_started_at = time.monotonic()
        definition = self.definition_service.load(
            account_id=req.account_id, app_id=req.app_id, invoke_from=req.invoke_from,
        )
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
        self._log_build_timing(req, "config", phase_started_at)

        # 2.按调用来源解析主模型；Assistant Agent 使用内置能力集合。
        phase_started_at = time.monotonic()
        if req.invoke_from is InvokeFrom.ASSISTANT_AGENT:
            account = self.db.session.get(Account, req.account_id)
            if account is None:
                raise NotFoundException("账号不存在")
            model_config = AppModelConfigReq.model_validate(
                self.language_model_service.get_default_model_config(account_id)
            )
        else:
            if app_config.model_config_data is None:
                raise ValidateErrorException("Agent 运行配置缺少模型")
            model_config = app_config.model_config_data
        if req.model_selection:
            model_config, _ = self.model_service.resolve(
                account_id=req.account_id, created_by=req.created_by,
                invoke_from=req.invoke_from, default_config=model_config,
                selection=req.model_selection, owner_account_id=account_id,
            )
        model_config = self.model_service.with_reasoning_override(model_config, req.reasoning_effort)
        model = self.language_model_service.load_language_model(model_config, account_id=account_id)
        self._log_build_timing(req, "model", phase_started_at)

        # 3.把应用配置中的普通工具、知识库和工作流转换为运行时工具。
        phase_started_at = time.monotonic()
        if req.invoke_from is InvokeFrom.ASSISTANT_AGENT:
            tools = self.assistant_agent_service.build_tools(account_id)
        else:
            tools = self._build_configured_tools(
                app_config,
                account_id=account_id,
                app_id=req.app_id,
            )
        self._log_build_timing(req, "tools", phase_started_at)

        # 4.解析同步和异步 SubAgent 的运行时绑定。
        phase_started_at = time.monotonic()
        subagents = self._prepare_subagents(
            app_config.subagents,
            account_id=account_id,
            parent_app_id=req.app_id,
            parent_model=cast(BaseChatModel, model),
            parent_tools=tools,
        )
        self._log_build_timing(req, "subagents", phase_started_at)

        # 5.授权 Skill 和运行上下文在同一份应用配置上解析，直接生成最终 Core 输入。
        context = AgentRunContext(
            account_id=req.account_id,
            app_id=req.app_id,
            conversation_id=req.conversation_id,
            created_by=req.created_by,
            principal_id=(req.end_user_id or req.created_by),
            memory_enabled=app_config.long_term_memory.enable,
            planning_enabled=req.planning_enabled,
            user_input_enabled=req.user_input_enabled and app_config.user_input.enable,
        )
        skills = self.skill_service.resolve_enabled_skills(app_config.skills)
        agent_config = AgentConfig(
            name=definition.name,
            description=definition.description,
            model=cast(BaseChatModel, model),
            preset_prompt=app_config.preset_prompt,
            tools=tuple(tools),
            skills=tuple(
                AgentSkill(
                    name=skill.name,
                    skill_md=skill.skill_md,
                    package_files=dict(skill.package_files),
                )
                for skill in skills
            ),
            selected_skills=selected_skills,
            subagents=tuple(subagents),
        )
        return agent_config, context

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
            url, headers = build_internal_agent_transport(
                settings=self.config,
                account_id=account_id, parent_app_id=parent_app_id,
                target_app_id=target_id, headers=headers,
            )
            graph_id = build_published_graph_id(target_id)
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
    ) -> None:
        """记录 Agent 装配子阶段耗时。"""
        # 1.计算当前装配阶段耗时。
        duration_ms = (time.monotonic() - started_at) * 1000

        # 2.输出可按应用和会话检索的结构化日志。
        logger.info(
            "agent_assembly_timing app_id=%s conversation_id=%s "
            "stage=%s duration_ms=%.2f",
            req.app_id,
            req.conversation_id,
            stage,
            duration_ms,
        )


__all__ = ["AgentFactory"]
