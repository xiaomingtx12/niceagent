"""统一装配并执行平台 Agent 调用。"""

from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from flask import current_app
from injector import inject
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from internal.core.agent_v2.agents import BaseAgent
from internal.core.agent_v2.agents.models import AgentThought, QueueEvent
from internal.core.agent_v2.events.serialization import serialize_agent_thought
from internal.core.agent_v2.runtime.configuration import (
    build_agent_config_from_app_config,
)
from internal.entity.app_entity import AppStatus
from internal.entity.conversation_entity import InvokeFrom
from internal.entity.dataset_entity import RetrievalSource
from internal.exception import FailException
from internal.model import App
from pkg.sqlalchemy import SQLAlchemy
from .agent_runtime_service import AgentRuntimeService
from .agent_attachment_service import AgentAttachment, AgentAttachmentService
from .app_config_service import AppConfigService
from .base_service import BaseService
from .conversation_service import ConversationService
from .language_model_service import LanguageModelService
from .retrieval_service import RetrievalService


@dataclass(frozen=True, slots=True)
class AgentInvocationSpec:
    """一次 Agent 调用所需的可信业务上下文。"""

    account_id: UUID
    app: App | None
    app_config: Mapping[str, Any]
    conversation_id: UUID
    message_id: UUID
    created_by: UUID
    user_id: UUID
    end_user_id: UUID | None
    invoke_from: InvokeFrom
    query: str
    image_urls: list[str]
    attachments: Sequence[AgentAttachment] = ()
    opening_statement: str = ""
    app_id: UUID | None = None
    extra_tools: Sequence[BaseTool] = ()
    llm: BaseChatModel | None = None
    app_name: str = ""
    app_description: str = ""
    input_message_id: str | None = None


@inject
@dataclass
class AgentInvocationService(BaseService):
    """统一处理 Agent 装配、流式事件聚合和轨迹持久化。"""

    db: SQLAlchemy
    app_config_service: AppConfigService
    retrieval_service: RetrievalService
    language_model_service: LanguageModelService
    agent_runtime_service: AgentRuntimeService
    conversation_service: ConversationService
    agent_attachment_service: AgentAttachmentService

    def build_agent(
        self,
        spec: AgentInvocationSpec,
    ) -> tuple[BaseAgent, BaseChatModel]:
        """根据业务配置构造一次性的 DeepAgents 运行实例。"""
        # 1.确定应用身份并加载本次调用使用的主模型。
        app_id = spec.app_id or (spec.app.id if spec.app else None)
        if app_id is None:
            raise ValueError("Agent 调用缺少 app_id")
        llm = spec.llm or self.language_model_service.load_language_model(
            spec.app_config.get("model_config", {}),
            account_id=spec.account_id,
        )
        # 2.把额外工具和应用配置中的工具转换成 LangChain 工具实例。
        tools = list(spec.extra_tools)
        tools.extend(
            self.app_config_service.get_langchain_tools_by_tools_config(
                spec.app_config.get("tools", []),
                account_id=spec.account_id,
            )
            )

        # 3.把知识库检索和工作流绑定转换为 Agent 可调用工具。
        if spec.app_config.get("datasets"):
            tools.append(
                self.retrieval_service.create_langchain_tool_from_search(
                    flask_app=current_app._get_current_object(),
                    dataset_ids=[
                        dataset["id"] for dataset in spec.app_config["datasets"]
                    ],
                    account_id=spec.account_id,
                    retrival_source=RetrievalSource.APP,
                    source_app_id=app_id,
                    **spec.app_config.get("retrieval_config", {}),
                )
            )

        if spec.app_config.get("workflows"):
            tools.extend(
                self.app_config_service.get_langchain_tools_by_workflow_ids(
                    [workflow["id"] for workflow in spec.app_config["workflows"]],
                    account_id=spec.account_id,
                )
            )

        # 4.解析 SubAgent 的模型、工具和发布应用引用。
        runtime_subagents, subagent_tools, subagent_models = (
            self._prepare_subagents(
                spec.app_config.get("subagents", []),
                account_id=spec.account_id,
            )
        )
        # 5.按会话作用域装配 Workspace、Skill、Memory、Sandbox 等资源。
        runtime = self.agent_runtime_service.prepare(
            account_id=spec.account_id,
            app_id=app_id,
            conversation_id=spec.conversation_id,
            created_by=spec.created_by,
            end_user_id=spec.end_user_id,
            skill_bindings=spec.app_config.get("skills", []),
            subagents=runtime_subagents,
        )
        runtime_app_config = dict(spec.app_config)
        runtime_app_config["subagents"] = runtime_subagents
        try:
            # 6.先把附件投影到 Workspace，再创建持有这些资源的 BaseAgent。
            self.agent_attachment_service.materialize(
                spec.attachments,
                runtime.backend,
            )
            agent = BaseAgent(
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
                agent_config=build_agent_config_from_app_config(
                    user_id=spec.user_id,
                    invoke_from=spec.invoke_from,
                    app_name=spec.app.name if spec.app else spec.app_name,
                    app_description=(
                        spec.app.description if spec.app else spec.app_description
                    ),
                    app_config=runtime_app_config,
                    tools=tools,
                    opening_statement=spec.opening_statement,
                ),
            )
        except Exception:
            # BaseAgent 通常会在构造失败时关闭 Backend；
            # 这里继续保留兜底，处理资源尚未交给 BaseAgent 前的异常。
            try:
                close = getattr(runtime.backend, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
            raise
        return agent, llm

    def _prepare_subagents(
        self,
        subagents: Sequence[dict[str, Any]],
        *,
        account_id: UUID,
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, list[BaseTool]],
        dict[str, BaseChatModel],
    ]:
        """把发布 Agent 引用解析为当前图可装配的原生同步子 Agent。"""
        # 1.复制持久化配置，并准备每个 SubAgent 的工具和模型覆盖。
        normalized = [dict(item) for item in subagents or []]
        tool_overrides: dict[str, list[BaseTool]] = {}
        model_overrides: dict[str, BaseChatModel] = {}

        for item in normalized:
            # 2.先解析显式模型配置，形成运行时模型覆盖。
            model_ref = item.get("model")
            if model_ref:
                provider_name = str(item.get("model_provider") or "").strip()
                model_name = str(model_ref).strip()
                if not provider_name:
                    model_parts = str(model_ref).split("/", 1)
                    if len(model_parts) != 2 or any(
                        not part.strip() for part in model_parts
                    ):
                        raise FailException(
                            f"子 Agent {item['name']!r} 的模型标识必须是 provider/model 格式"
                        )
                    provider_name, model_name = (
                        part.strip() for part in model_parts
                    )
                model_overrides[item["name"]] = (
                    self.language_model_service.load_language_model(
                        {
                            "provider": provider_name,
                            "model": model_name,
                            "provider_connection_id": item.get("model_connection_id"),
                        },
                        account_id=account_id,
                    )
                )

            if item.get("source", "custom") != "published":
                continue

            # 3.校验发布应用属于当前租户且仍处于发布状态。
            target_id = UUID(str(item["published_app_id"]))
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
                raise FailException(
                    f"子 Agent 引用的发布应用 {target_id} 不存在或已取消发布"
                )

            # 4.加载发布应用的工具、知识库和工作流能力。
            target_config = self.app_config_service.get_app_config(target_app)
            target_tools = self.app_config_service.get_langchain_tools_by_tools_config(
                target_config.get("tools", []),
                account_id=account_id,
            )
            if target_config.get("datasets"):
                target_tools.append(
                    self.retrieval_service.create_langchain_tool_from_search(
                        flask_app=current_app._get_current_object(),
                        dataset_ids=[
                            dataset["id"] for dataset in target_config["datasets"]
                        ],
                        account_id=account_id,
                        retrival_source=RetrievalSource.APP,
                        source_app_id=target_id,
                        **target_config.get("retrieval_config", {}),
                    )
                )
            if target_config.get("workflows"):
                target_tools.extend(
                    self.app_config_service.get_langchain_tools_by_workflow_ids(
                        [workflow["id"] for workflow in target_config["workflows"]],
                        account_id=account_id,
                    )
                )

            # 5.拒绝工具冲突，并把发布应用转换为同步 SubAgent 配置。
            tool_names = [tool.name for tool in target_tools]
            if len(tool_names) != len(set(tool_names)):
                raise FailException(
                    f"发布 Agent {target_app.name!r} 的工具名称存在冲突"
                )
            reserved_tools = {
                "task",
                "write_todos",
                "ls",
                "read_file",
                "write_file",
                "edit_file",
                "glob",
                "grep",
                "execute",
            }
            if reserved_tools.intersection(tool_names):
                raise FailException(
                    f"发布 Agent {target_app.name!r} 的工具名称与 DeepAgents 内置工具冲突"
                )

            target_model = self.language_model_service.load_language_model(
                target_config.get("model_config", {}),
                account_id=account_id,
            )
            item["mode"] = "sync"
            item["context_passing"] = "NONE"
            item["system_prompt"] = (
                f"你是已发布应用「{target_app.name}」的执行 Agent。"
                "请严格遵循该应用的发布人设完成任务，并只返回本次任务的最终结果。\n\n"
                f"{target_config.get('preset_prompt', '')}"
            ).strip()
            item["tools"] = tool_names
            item["skills"] = []
            tool_overrides[item["name"]] = target_tools
            model_overrides[item["name"]] = target_model

        # 6.返回图构建阶段所需的规范化配置和运行时覆盖项。
        return normalized, tool_overrides, model_overrides

    def stream(
        self,
        spec: AgentInvocationSpec,
    ) -> Generator[AgentThought, None, None]:
        """流式执行 Agent，并在正常结束后统一持久化轨迹。"""
        try:
            agent, llm = self.build_agent(spec)
        except Exception as error:
            return self._error_stream(spec, error)

        # 先完成 build_agent，再返回真正的生成器；构建失败会转成统一错误事件。
        def generate() -> Generator[AgentThought, None, None]:
            thought_map: dict[str, AgentThought] = {}
            completed = False
            try:
                input_message = llm.convert_to_human_message(
                    spec.query,
                    spec.image_urls,
                    [attachment.to_dict() for attachment in spec.attachments],
                )
                if spec.input_message_id:
                    input_message.id = spec.input_message_id
                input_state = {"messages": [input_message]}
                for thought in agent.stream(input_state):
                    self._collect_thought(thought_map, thought)
                    yield thought
                completed = True
            except Exception as error:
                error_thought = self._build_error_thought(error)
                self._persist_error(
                    spec,
                    error_thought,
                    run_usage=agent.last_run_usage,
                )
                yield error_thought
            finally:
                if completed:
                    self._persist(
                        spec,
                        thought_map.values(),
                        run_usage=agent.last_run_usage,
                    )

        return generate()

    def _error_stream(
        self,
        spec: AgentInvocationSpec,
        error: Exception,
    ) -> Generator[AgentThought, None, None]:
        """把 Agent 构建失败转换为前端可消费的 SSE 错误事件。"""

        def generate() -> Generator[AgentThought, None, None]:
            error_thought = self._build_error_thought(error)
            self._persist_error(spec, error_thought)
            yield error_thought

        return generate()

    def _persist_error(
        self,
        spec: AgentInvocationSpec,
        error_thought: AgentThought,
        run_usage: Mapping[str, Any] | None = None,
    ) -> None:
        """尽力持久化 Agent 错误事件，避免覆盖原始执行异常。"""
        try:
            self._persist(spec, [error_thought], run_usage=run_usage)
        except Exception:
            current_app.logger.exception("持久化 Agent 错误事件失败")

    @staticmethod
    def _build_error_thought(error: Exception) -> AgentThought:
        """把异常转换为统一的 Agent 错误轨迹对象。"""
        if isinstance(error, FailException):
            message = error.message.strip() or "Agent 执行失败"
            current_app.logger.warning("Agent 执行失败: %s", message)
        else:
            message = str(error).strip() or "Agent 执行失败"
            current_app.logger.error(
                "Agent 执行失败",
                exc_info=(type(error), error, error.__traceback__),
            )

        return AgentThought(
            id=uuid4(),
            task_id=uuid4(),
            event=QueueEvent.ERROR,
            observation=message,
        )

    def invoke(self, spec: AgentInvocationSpec):
        """非流式执行 Agent，并统一持久化轨迹。"""
        # 1.构造本次调用的 Agent 和模型适配器。
        agent, llm = self.build_agent(spec)
        # 2.把业务请求转换为模型可接受的 HumanMessage。
        input_message = llm.convert_to_human_message(
            spec.query,
            spec.image_urls,
            [attachment.to_dict() for attachment in spec.attachments],
        )
        if spec.input_message_id:
            input_message.id = spec.input_message_id
        # 3.执行非流式 Agent 调用并聚合最终结果。
        result = agent.invoke({"messages": [input_message]})
        # 4.把推理轨迹和本次运行用量统一写回会话消息。
        self._persist(
            spec,
            result.agent_thoughts,
            run_usage=agent.last_run_usage,
        )
        return result

    def serialize_event(
        self,
        thought: AgentThought,
        *,
        conversation_id: UUID,
        message_id: UUID,
        end_user_id: UUID | None = None,
    ) -> dict[str, Any]:
        """生成不包含传输层事件名的 SSE 数据负载。"""
        payload = {
            **serialize_agent_thought(thought),
            "id": str(thought.id),
            "conversation_id": str(conversation_id),
            "message_id": str(message_id),
            "task_id": str(thought.task_id),
        }
        if end_user_id is not None:
            payload["end_user_id"] = str(end_user_id)
        return payload

    def _persist(
        self,
        spec: AgentInvocationSpec,
        thoughts: Sequence[AgentThought],
        run_usage: Mapping[str, Any] | None = None,
    ) -> None:
        """把一次 Agent 调用的轨迹和 Run 用量交给会话服务持久化。"""
        # 1.由 ConversationService 统一处理轨迹、消息状态和 Run 用量。
        self.conversation_service.save_agent_thoughts(
            account_id=spec.account_id,
            app_id=spec.app_id or spec.app.id,
            app_config=dict(spec.app_config),
            conversation_id=spec.conversation_id,
            message_id=spec.message_id,
            agent_thoughts=list(thoughts),
            run_usage=run_usage,
        )

    @staticmethod
    def _collect_thought(
        thought_map: dict[str, AgentThought],
        thought: AgentThought,
    ) -> None:
        """按事件 id 聚合流式消息分片。"""
        if thought.event == QueueEvent.PING:
            return

        event_id = str(thought.id)
        previous = thought_map.get(event_id)
        if previous is None or thought.event != QueueEvent.AGENT_MESSAGE:
            thought_map[event_id] = thought
            return

        thought_map[event_id] = previous.model_copy(
            update={
                "thought": previous.thought + thought.thought,
                "message": thought.message,
                "message_token_count": thought.message_token_count,
                "message_unit_price": thought.message_unit_price,
                "message_price_unit": thought.message_price_unit,
                "answer": previous.answer + thought.answer,
                "answer_token_count": thought.answer_token_count,
                "answer_unit_price": thought.answer_unit_price,
                "answer_price_unit": thought.answer_price_unit,
                "total_token_count": thought.total_token_count,
                "total_price": thought.total_price,
                "latency": thought.latency,
            }
        )


__all__ = ["AgentInvocationService", "AgentInvocationSpec"]
