"""把业务侧 AgentInvocation 转换为一次性的 Agent 执行实例。"""

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
from pydantic import ValidationError

from config.config import Config
from internal.core.language_model.entities.model_entity import BaseLanguageModel
from internal.core.agent_v2.assembly.graph.deep_agent_factory import build_deep_agent
from internal.core.agent_v2.assembly.graph.subagent_spec_adapter import (
    SubAgentRuntimeBinding,
)
from internal.core.agent_v2.assembly.observability.run_observer import (
    PlatformAgentRunObserver,
)
from internal.core.agent_v2.entity.subagent_config import (
    SubAgentConfig,
    build_published_graph_id,
)
from internal.core.agent_v2.execution.agent_executor import AgentExecutor
from internal.core.agent_v2.protocol.internal_auth import (
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
from internal.model import Account, App
from internal.schema.agent_runtime_schema import AgentInvocation
from internal.schema.agent_attachment_schema import dump_agent_attachments
from internal.schema.app_config_schema import AgentAppConfigReq, AppModelConfigReq
from pkg.sqlalchemy import SQLAlchemy
from .agent_attachment_service import AgentAttachmentService
from .agent_runtime_factory import AgentRuntimeFactory
from .app_config_service import AppConfigService
from .assistant_agent_service import AssistantAgentService
from .language_model_service import LanguageModelService
from .retrieval_service import RetrievalService


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
    """统一完成业务配置到 Agent Core 的装配。

    本服务在执行边界读取草稿或发布配置并解析应用能力；运行时资源
    由 ``AgentRuntimeFactory`` 持有，``AgentWorkbenchService`` 仍负责
    Run、事件和取消。真正的图构建与模型/工具调度由 Agent Core 完成。
    """

    db: SQLAlchemy
    app_config_service: AppConfigService
    retrieval_service: RetrievalService
    language_model_service: LanguageModelService
    agent_attachment_service: AgentAttachmentService
    assistant_agent_service: AssistantAgentService
    runtime_factory: AgentRuntimeFactory
    config: Config

    # -------------------- Core assembly flow --------------------

    def build_agent(
        self,
        req: AgentInvocation,
        *,
        observer: PlatformAgentRunObserver,
    ) -> tuple[AgentExecutor, BaseMessage]:
        """根据一次调用的业务输入构造一次性的 ``AgentExecutor``。

        返回的执行器和输入消息只对应当前 Run，不能作为可复用实例保存。
        """
        # 1.读取并解析本次 Run 的应用定义、模型、工具和 SubAgent。
        build_started_at = time.monotonic()
        definition = self._resolve_definition(req, observer)

        # 2.将业务输入转换为模型可消费的 LangChain 消息。
        input_message = definition.model.convert_to_human_message(
            req.query,
            req.image_urls,
            dump_agent_attachments(req.attachments),
        )
        if req.input_message_id:
            input_message.id = req.input_message_id
        phase_started_at = time.monotonic()

        # 3.创建本次 Run 独占的 Workspace、Memory、Skill、Sandbox 等资源。
        try:
            runtime = self.runtime_factory.create(
                invocation=req,
                app_config=definition.config,
                selected_skills=definition.selected_skills,
                subagents=definition.subagents,
                observer=observer,
            )
        except ValueError as exc:
            raise ValidateErrorException(f"Agent 运行配置无效: {exc}") from exc
        self._log_build_timing(req, "runtime", phase_started_at, observer)
        phase_started_at = time.monotonic()

        # 4.将附件投影到 Workspace，供 Agent 文件工具读取。
        try:
            self.agent_attachment_service.materialize(
                req.attachments,
                runtime.backend,
            )
        except Exception:
            # 附件投影失败时执行器尚未接管资源；关闭操作不得覆盖原异常。
            self.runtime_factory.close(runtime)
            raise
        self._log_build_timing(req, "attachments", phase_started_at, observer)
        phase_started_at = time.monotonic()

        # 5.将已解析定义和运行时资源编译为 Deep Agents 图。
        try:
            graph = build_deep_agent(
                llm=cast(BaseChatModel, definition.model),
                app_name=definition.name,
                app_description=definition.description,
                preset_prompt=definition.config.preset_prompt,
                review_config=definition.config.review_config.model_dump(mode="json"),
                tools=definition.tools,
                subagents=definition.subagents,
                runtime=runtime,
                subagent_bindings=definition.subagent_bindings,
            )
            agent = AgentExecutor(
                graph=graph,
                runtime=runtime,
                observer=observer,
            )
        except ValueError as exc:
            self.runtime_factory.close(runtime)
            raise ValidateErrorException(f"Agent 运行配置无效: {exc}") from exc
        except Exception:
            self.runtime_factory.close(runtime)
            raise
        self._log_build_timing(req, "graph", phase_started_at, observer)

        # 6.交出资源生命周期，返回当前 Run 的一次性执行器和输入消息。
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
        app_name, app_description, account_id, app_config = (
            self._load_app_configuration(req)
        )
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
            model, tools = self.assistant_agent_service.build_capabilities(account)
        else:
            if app_config.model_config_data is None:
                raise ValidateErrorException("Agent 运行配置缺少模型")
            model = self.language_model_service.load_language_model(
                app_config.model_config_data,
                account_id=account_id,
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

    def _load_app_configuration(
        self,
        req: AgentInvocation,
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
            app_config = (
                AgentAppConfigReq.model_validate(
                    self.app_config_service.get_draft_app_config(app)
                )
                if req.invoke_from is InvokeFrom.DEBUGGER
                else self.app_config_service.get_agent_runtime_config(app)
            )
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
    ) -> tuple[
        list[SubAgentConfig],
        dict[str, SubAgentRuntimeBinding],
    ]:
        """把发布 Agent 引用解析为当前图可装配的原生同步子 Agent。"""
        # 1.批量读取发布引用，避免每个 SubAgent 重复访问数据库。
        published_apps = self._load_published_apps(
            subagents,
            account_id=account_id,
        )
        sync_app_ids = tuple(
            dict.fromkeys(
                config.published_app_id
                for config in subagents
                if config.source == "published"
                and config.mode == "sync"
                and config.published_app_id is not None
            )
        )
        published_configs = self.app_config_service.get_agent_runtime_configs(
            [published_apps[app_id] for app_id in sync_app_ids]
        )

        # 2.准备规范化配置和逐 SubAgent 的运行时依赖绑定。
        normalized: list[SubAgentConfig] = []
        bindings: dict[str, SubAgentRuntimeBinding] = {}

        # 3.逐个解析自定义、同步发布和异步发布 SubAgent。
        for config in subagents:
            name = config.name

            # 4.自定义 SubAgent 可选用独立模型，否则由 DeepAgents 继承主模型。
            if config.source != "published":
                normalized.append(config.model_copy(deep=True))
                model = self._load_subagent_model(config, account_id=account_id)
                if model is not None:
                    bindings[name] = SubAgentRuntimeBinding(model=model)
                continue

            # 5.发布 SubAgent 必须属于当前租户且仍处于发布状态。
            target_id = config.published_app_id
            if target_id is None:
                raise ValidateErrorException(f"子 Agent {name!r} 缺少发布应用标识")
            target_app = published_apps[target_id]

            # 6.异步发布 Agent 通过内部 Agent Protocol 独立执行。
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

            # 7.同步发布 Agent 加载目标应用的工具、知识库和工作流能力。
            target_config = published_configs[target_id]
            target_tools = self._build_configured_tools(
                target_config,
                account_id=account_id,
                app_id=target_id,
            )

            # 8.把发布应用转换为当前图可使用的同步 SubAgent 配置。
            # 工具名称冲突和 DeepAgents 保留名称由 Core 的
            # ``build_subagent_specs`` 统一校验；Service 只负责加载工具。
            tool_names = [tool.name for tool in target_tools]

            if target_config.model_config_data is None:
                raise ValidateErrorException(
                    f"发布 Agent {target_app.name!r} 缺少模型配置"
                )
            target_model = cast(
                BaseChatModel,
                self.language_model_service.load_language_model(
                    target_config.model_config_data,
                    account_id=account_id,
                ),
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
            bindings[name] = SubAgentRuntimeBinding(
                tools=tuple(target_tools),
                model=target_model,
            )

        # 9.返回图构建阶段所需的规范化配置和逐 Agent 运行时绑定。
        return normalized, bindings

    # -------------------- Internal assembly helpers --------------------

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
        target_ids = tuple(
            dict.fromkeys(
                config.published_app_id
                for config in subagents
                if config.source == "published"
                and config.published_app_id is not None
            )
        )
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
        missing = [
            target_id for target_id in target_ids if target_id not in apps_by_id
        ]
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
        # 1.没有模型覆盖时，让 DeepAgents 继承主 Agent 模型。
        model_ref = subagent.model
        if not model_ref:
            return None

        # 2.解析 provider/model 引用并加载模型实例。
        provider_name, model_name = model_ref.split("/", 1)
        return cast(
            BaseChatModel,
            self.language_model_service.load_language_model(
                AppModelConfigReq(
                    provider=provider_name,
                    model=model_name,
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
