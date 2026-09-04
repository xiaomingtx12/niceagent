"""把业务侧 AgentInvocation 转换为一次性的 Agent 执行实例。"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from flask import current_app
from injector import inject
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore
from sqlalchemy.orm import Session, sessionmaker

from config.config import Config
from internal.core.agent_v2.assembly.context.memory_context import (
    MemoryContextAssembler,
)
from internal.core.agent_v2.assembly.context.sandbox_context import (
    SandboxContextAssembler,
    SandboxProjection,
    build_filesystem_permissions,
)
from internal.core.agent_v2.assembly.context.skill_context import SkillContextAssembler
from internal.core.agent_v2.assembly.context.workspace_context import (
    WorkspaceContextAssembler,
    WorkspaceProjection,
)
from internal.core.agent_v2.assembly.observability.telemetry import (
    AgentTelemetry,
    LoggingAgentTelemetrySink,
)
from internal.core.agent_v2.backends.composition.composite_backend_factory import (
    build_composite_backend,
    close_runtime_backends,
)
from internal.core.agent_v2.backends.sandbox.sandbox_backend_factory import (
    build_stable_sandbox_name,
)
from internal.core.agent_v2.entity.agent_config import AgentConfig
from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.entity.subagent_config import (
    SubAgentConfig,
    build_published_graph_id,
)
from internal.core.agent_v2.execution.agent_executor import AgentExecutor
from internal.core.agent_v2.persistence.langgraph_checkpoint_provider import (
    get_configured_checkpointer,
)
from internal.core.agent_v2.persistence.langgraph_store_provider import (
    get_configured_store,
)
from internal.core.agent_v2.protocol.internal_auth import (
    issue_internal_agent_protocol_token,
)
from internal.entity.app_entity import AppStatus
from internal.entity.conversation_entity import InvokeFrom
from internal.entity.dataset_entity import RetrievalSource
from internal.exception import (
    AgentInvocationException,
    ValidateErrorException,
)
from internal.extension.logging_extension import logger
from internal.model import App
from internal.schema.agent_runtime_schema import (
    AgentInvocation,
    AgentRuntimePrepareReq,
)
from internal.schema.app_config_schema import AgentAppConfigReq, AppModelConfigReq
from pkg.sqlalchemy import SQLAlchemy
from .agent_attachment_service import AgentAttachmentService
from .app_config_service import AppConfigService
from .base_service import BaseService
from .language_model_service import LanguageModelService
from .retrieval_service import RetrievalService
from .skill_service import BuiltinSkill, SkillService
from .workspace_file_storage_service import WorkspaceFileStorageService

@dataclass(frozen=True, slots=True)
class _AgentRuntimeResources:
    """AgentAssemblyService 交给 AgentExecutor 的一次运行时资源快照。"""

    checkpointer: BaseCheckpointSaver | bool
    backend: BackendProtocol
    permissions: list[FilesystemPermission]
    skill_sources: list[str]
    memory_sources: list[str]
    subagent_skill_sources: Mapping[str, list[str]]
    telemetry: AgentTelemetry
    runtime_context: AgentRunContext
    store: BaseStore | None = None


@inject
@dataclass
class AgentAssemblyService(BaseService):
    """统一完成业务配置到 Agent Core 的装配。

    本服务负责加载租户可用的模型、工具、知识库和发布 SubAgent，并
    由本服务内部准备运行时资源；``AgentWorkbenchService``
    仍负责 Run、事件和取消。真正的图构建与模型/工具调度由 Agent Core 完成。
    """

    db: SQLAlchemy
    app_config_service: AppConfigService
    retrieval_service: RetrievalService
    language_model_service: LanguageModelService
    agent_attachment_service: AgentAttachmentService
    skill_service: SkillService
    workspace_file_storage_service: WorkspaceFileStorageService
    config: Config

    def build_agent(
        self,
        req: AgentInvocation,
    ) -> tuple[AgentExecutor, BaseChatModel]:
        """根据一次调用的业务输入构造一次性的 ``AgentExecutor``。

        返回的执行器只对应当前 Run，不能作为数据库中的可复用实例保存。
        """
        build_started_at = time.monotonic()
        phase_started_at = build_started_at
        selected_skills = self._validate_selected_skills(
            req.app_config,
            req.selected_skills,
        )

        # 1.加载本次调用使用的主模型。
        llm = req.llm
        if llm is None:
            if req.app_config.model_config_data is None:
                raise AgentInvocationException("Agent 运行配置缺少模型")
            llm = self.language_model_service.load_language_model(
                req.app_config.model_config_data,
                account_id=req.configuration_account_id,
            )
        self._log_build_timing(req, "model", phase_started_at)
        phase_started_at = time.monotonic()

        # 2.把额外工具和应用配置中的工具转换成 LangChain 工具实例。
        tools = list(req.extra_tools)
        tools.extend(
            self._build_configured_tools(
                req.app_config,
                account_id=req.configuration_account_id,
                app_id=req.app_id,
            )
        )
        self._log_build_timing(req, "tools", phase_started_at)
        phase_started_at = time.monotonic()

        # 3.解析 SubAgent 的模型、工具和发布应用引用。
        runtime_subagents, subagent_tools, subagent_models = self._prepare_subagents(
            req.app_config.subagents,
            account_id=req.configuration_account_id,
            parent_app_id=req.app_id,
        )
        self._log_build_timing(req, "subagents", phase_started_at)
        phase_started_at = time.monotonic()

        # 4.按用户和会话作用域装配 Workspace、Skill、Memory、Sandbox 等资源。
        runtime = self._prepare_runtime(
            AgentRuntimePrepareReq(
                account_id=req.account_id,
                app_id=req.app_id,
                conversation_id=req.conversation_id,
                created_by=req.created_by,
                principal_id=(req.end_user_id or req.user_id or req.created_by),
                memory_enabled=req.app_config.long_term_memory.enable,
                skill_bindings=req.app_config.skills,
                selected_skills=selected_skills,
                subagents=runtime_subagents,
            )
        )
        self._log_build_timing(req, "runtime", phase_started_at)
        phase_started_at = time.monotonic()

        try:
            # 5.先把附件投影到 Workspace；此时资源仍由装配服务持有。
            self.agent_attachment_service.materialize(
                req.attachments,
                runtime.backend,
            )
        except Exception:
            # 附件投影失败时执行器尚未接管资源；关闭操作不得覆盖原异常。
            self._close_unowned_runtime(runtime.backend)
            raise
        self._log_build_timing(req, "attachments", phase_started_at)
        phase_started_at = time.monotonic()

        # AgentExecutor 构造失败时会自行关闭 Backend，避免这里重复释放。
        agent = AgentExecutor(
            llm=llm,
            checkpointer=runtime.checkpointer,
            backend=runtime.backend,
            store=runtime.store,
            permissions=runtime.permissions,
            skill_sources=runtime.skill_sources,
            memory_sources=runtime.memory_sources,
            subagent_skill_sources=runtime.subagent_skill_sources,
            subagent_tools_by_name=subagent_tools,
            subagent_models_by_name=subagent_models,
            telemetry=runtime.telemetry,
            runtime_context=runtime.runtime_context,
            agent_config=self._build_agent_config(
                user_id=req.user_id,
                invoke_from=req.invoke_from,
                app_name=req.app_name,
                app_description=req.app_description,
                app_config=req.app_config,
                subagents=runtime_subagents,
                tools=tools,
                opening_statement=req.opening_statement,
                selected_skills=selected_skills,
            ),
        )
        self._log_build_timing(req, "graph", phase_started_at)
        self._log_build_timing(req, "total", build_started_at)
        return agent, llm

    @staticmethod
    def _validate_selected_skills(
        app_config: AgentAppConfigReq,
        selected_skills: Sequence[str],
    ) -> tuple[str, ...]:
        """确认客户端选择的 Skill 已在当前运行配置中挂载并启用。"""
        if not selected_skills:
            return ()
        enabled_names = {
            binding.name for binding in app_config.skills if binding.enabled
        }
        missing = sorted(set(selected_skills) - enabled_names)
        if missing:
            raise ValidateErrorException(
                f"所选 Skill 未挂载或未启用: {', '.join(missing)}"
            )
        return tuple(selected_skills)

    @staticmethod
    def _log_build_timing(
        req: AgentInvocation,
        stage: str,
        started_at: float,
    ) -> None:
        """记录 Agent 装配子阶段耗时。"""
        logger.info(
            "agent_assembly_timing app_id=%s conversation_id=%s stage=%s duration_ms=%.2f",
            req.app_id,
            req.conversation_id,
            stage,
            (time.monotonic() - started_at) * 1000,
        )

    @staticmethod
    def _close_unowned_runtime(backend: BackendProtocol | None) -> None:
        """清理尚未交给 AgentExecutor 的资源，且不覆盖原始异常。"""
        try:
            close_runtime_backends(backend)
        except Exception:
            logger.exception("关闭未接管的 Agent Backend 失败")

    def _prepare_runtime(
        self,
        req: AgentRuntimePrepareReq,
    ) -> _AgentRuntimeResources:
        """准备本次 Run 独占的 Workspace、Skill、Memory 和 Sandbox 资源。"""
        skills = self.skill_service.list_enabled_latest(
            account_id=req.account_id,
            app_id=req.app_id,
            bindings=list(req.skill_bindings),
        )
        settings = self.config
        runtime_session = sessionmaker(
            bind=self.db.engine,
            expire_on_commit=False,
        )()
        try:
            return self._assemble_runtime_resources(
                runtime_session=runtime_session,
                req=req,
                skills=skills,
                settings=settings,
            )
        except Exception:
            runtime_session.close()
            raise

    def _assemble_runtime_resources(
        self,
        *,
        runtime_session: Session,
        req: AgentRuntimePrepareReq,
        skills: list[BuiltinSkill],
        settings: Config,
    ) -> _AgentRuntimeResources:
        """在装配服务内创建 Agent Core 所需的运行时资源集合。"""
        run_context = AgentRunContext(
            account_id=req.account_id,
            app_id=req.app_id,
            conversation_id=req.conversation_id,
            created_by=req.created_by,
            principal_id=req.principal_id,
            memory_enabled=req.memory_enabled,
        )
        telemetry = AgentTelemetry(
            LoggingAgentTelemetrySink(),
            attributes=run_context.telemetry_attributes,
        )
        workspace_projection: WorkspaceProjection | None = None
        sandbox_projection: SandboxProjection | None = None
        try:
            workspace_projection = WorkspaceContextAssembler().build(
                runtime_session=runtime_session,
                run_context=run_context,
                settings=settings,
                telemetry=telemetry,
                storage_service=self.workspace_file_storage_service,
            )
            skill_projection = SkillContextAssembler().build(
                skills=skills,
                selected_skill_names=req.selected_skills,
                subagents=[
                    item.model_dump(mode="json", exclude_none=True)
                    for item in req.subagents
                ],
            )
            memory_projection = MemoryContextAssembler(
                store_provider=get_configured_store,
            ).build(enabled=run_context.memory_enabled)
            sandbox_projection = SandboxContextAssembler().build(
                settings=settings,
                workspace=workspace_projection.backend,
                sandbox_name=build_stable_sandbox_name(
                    account_id=run_context.account_id,
                    app_id=run_context.app_id,
                    conversation_id=run_context.conversation_id,
                    agent_id=run_context.agent_id,
                ),
                skill_projection=skill_projection,
                telemetry=telemetry,
            )
            backend = build_composite_backend(
                routes={
                    **workspace_projection.routes,
                    **skill_projection.routes,
                    **memory_projection.routes,
                },
                sandbox=(
                    sandbox_projection.backend
                    if sandbox_projection is not None
                    else None
                ),
            )
            checkpointer = get_configured_checkpointer()
        except Exception:
            # 装配中途失败时两个 Backend 都可能已经创建；清理异常不能覆盖主异常。
            for backend_to_close in (
                sandbox_projection.backend if sandbox_projection is not None else None,
                (
                    workspace_projection.backend
                    if workspace_projection is not None
                    else None
                ),
            ):
                if backend_to_close is not None:
                    self._close_unowned_runtime(backend_to_close)
            raise

        return _AgentRuntimeResources(
            checkpointer=checkpointer,
            backend=backend,
            permissions=build_filesystem_permissions(
                memory_enabled=run_context.memory_enabled,
            ),
            skill_sources=skill_projection.sources,
            memory_sources=memory_projection.sources,
            subagent_skill_sources=skill_projection.subagent_sources,
            telemetry=telemetry,
            store=memory_projection.store,
            runtime_context=run_context,
        )

    @staticmethod
    def _build_agent_config(
        *,
        user_id: UUID,
        invoke_from: InvokeFrom,
        app_name: str,
        app_description: str,
        app_config: AgentAppConfigReq,
        subagents: Sequence[SubAgentConfig],
        tools: list[BaseTool],
        opening_statement: str = "",
        selected_skills: Sequence[str] = (),
    ) -> AgentConfig:
        """把已处理的 AppConfig 转成 Agent 核心配置。"""
        return AgentConfig(
            user_id=user_id,
            invoke_from=invoke_from,
            app_name=app_name,
            app_description=app_description,
            preset_prompt=app_config.preset_prompt,
            opening_statement=opening_statement,
            subagents=list(subagents),
            tools=list(tools),
            review_config=app_config.review_config.model_dump(mode="json"),
            max_model_calls=app_config.max_model_calls,
            max_tool_calls=app_config.max_tool_calls,
            memory_enabled=app_config.long_term_memory.enable,
            selected_skills=tuple(selected_skills),
        )

    def _build_configured_tools(
        self,
        app_config: AgentAppConfigReq,
        *,
        account_id: UUID,
        app_id: UUID,
    ) -> list[BaseTool]:
        """把应用的工具、知识库和工作流配置转换为运行时工具。"""
        tools = self.app_config_service.get_langchain_tools_by_tools_config(
            app_config.dump_tools(),
            account_id=account_id,
        )
        if app_config.datasets:
            tools.append(
                self.retrieval_service.create_langchain_tool_from_search(
                    flask_app=current_app._get_current_object(),
                    dataset_ids=[dataset.id for dataset in app_config.datasets],
                    account_id=account_id,
                    retrival_source=RetrievalSource.APP,
                    source_app_id=app_id,
                    **app_config.retrieval_config.model_dump(mode="python"),
                )
            )
        if app_config.workflows:
            tools.extend(
                self.app_config_service.get_langchain_tools_by_workflow_ids(
                    [workflow.id for workflow in app_config.workflows],
                    account_id=account_id,
                )
            )
        return tools

    def _prepare_subagents(
        self,
        subagents: Sequence[SubAgentConfig],
        *,
        account_id: UUID,
        parent_app_id: UUID,
    ) -> tuple[
        list[SubAgentConfig],
        dict[str, list[BaseTool]],
        dict[str, BaseChatModel],
    ]:
        """把发布 Agent 引用解析为当前图可装配的原生同步子 Agent。"""
        # 1.复制持久化配置，并准备每个 SubAgent 的工具和模型覆盖。
        normalized: list[SubAgentConfig] = []
        tool_overrides: dict[str, list[BaseTool]] = {}
        model_overrides: dict[str, BaseChatModel] = {}

        for config in subagents:
            name = config.name

            # 2.自定义 SubAgent 可以覆盖模型；发布引用始终使用目标应用模型。
            if config.source != "published":
                normalized.append(config.model_copy(deep=True))
                model = self._load_subagent_model(config, account_id=account_id)
                if model is not None:
                    model_overrides[name] = model
                continue

            # 3.校验发布应用属于当前租户且仍处于发布状态。
            target_id = config.published_app_id
            if target_id is None:
                raise AgentInvocationException(f"子 Agent {name!r} 缺少发布应用标识")
            target_app = (
                self.db.session.query(App)
                .filter(
                    App.id == target_id,
                    App.account_id == account_id,
                    App.status == AppStatus.PUBLISHED,
                )
                .one_or_none()
            )
            if target_app is None:
                raise AgentInvocationException(
                    f"子 Agent 引用的发布应用 {target_id} 不存在或已取消发布"
                )

            # 异步发布 Agent 由独立 Workbench Run 执行，不在父图内加载目标模型、工具和 Skill。
            if config.mode == "async":
                internal_transport = self._build_internal_async_transport(
                    account_id=account_id,
                    parent_app_id=parent_app_id,
                    target_app_id=target_id,
                    headers=config.headers,
                )
                normalized.append(
                    config.model_copy(
                        deep=True,
                        update={
                            "graph_id": build_published_graph_id(target_id),
                            **internal_transport,
                        },
                    )
                )
                continue

            # 4.加载发布应用的工具、知识库和工作流能力。
            try:
                target_config = self.app_config_service.get_agent_runtime_config(
                    target_app
                )
            except ValidateErrorException as exc:
                raise AgentInvocationException(
                    f"发布 Agent {target_app.name!r} 的运行配置无效: {exc}"
                ) from exc
            target_tools = self._build_configured_tools(
                target_config,
                account_id=account_id,
                app_id=target_id,
            )

            # 5.把发布应用转换为同步 SubAgent 配置。
            # 工具名称冲突和 DeepAgents 保留名称由 Core 的
            # ``build_subagent_specs`` 统一校验；Service 只负责加载工具。
            tool_names = [tool.name for tool in target_tools]

            if target_config.model_config_data is None:
                raise AgentInvocationException(
                    f"发布 Agent {target_app.name!r} 缺少模型配置"
                )
            target_model = self.language_model_service.load_language_model(
                target_config.model_config_data,
                account_id=account_id,
            )
            normalized.append(
                config.model_copy(
                    deep=True,
                    update={
                        "mode": "sync",
                        "context_passing": "NONE",
                        "system_prompt": (
                            f"你是已发布应用「{target_app.name}」的执行 Agent。"
                            "请严格遵循该应用的发布人设完成任务，并只返回本次任务的最终结果。\n\n"
                            f"{target_config.preset_prompt}"
                        ).strip(),
                        "tools": tool_names,
                        "skills": [],
                    },
                )
            )
            tool_overrides[name] = target_tools
            model_overrides[name] = target_model

        # 6.返回图构建阶段所需的规范化配置和运行时覆盖项。
        return (
            normalized,
            tool_overrides,
            model_overrides,
        )

    def _build_internal_async_transport(
        self,
        *,
        account_id: UUID,
        parent_app_id: UUID,
        target_app_id: UUID,
        headers: dict[str, str],
    ) -> dict[str, object]:
        """为平台内部发布 Agent 绑定 URL 和不可伪造的作用域凭证。"""
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
        # 禁止持久化配置伪造内部凭证；大小写不同的 HTTP Header 也必须移除，
        # 否则 HTTP 客户端可能发送重复 Header，导致服务端解析结果不确定。
        safe_headers = {
            key: value
            for key, value in headers.items()
            if key.lower() != "x-mydify-internal-token"
        }
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
        model_ref = subagent.model
        if not model_ref:
            return None
        provider_name, model_name = model_ref.split("/", 1)
        return self.language_model_service.load_language_model(
            AppModelConfigReq(
                provider=provider_name,
                model=model_name,
                parameters={},
            ),
            account_id=account_id,
        )


__all__ = ["AgentAssemblyService"]
