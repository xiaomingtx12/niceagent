"""统一装配并执行平台 Agent 调用。"""

from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

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
from internal.entity.conversation_entity import InvokeFrom
from internal.entity.dataset_entity import RetrievalSource
from internal.model import App
from pkg.sqlalchemy import SQLAlchemy
from .agent_runtime_service import AgentRuntimeService
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
    opening_statement: str = ""
    app_id: UUID | None = None
    extra_tools: Sequence[BaseTool] = ()
    llm: BaseChatModel | None = None
    app_name: str = ""
    app_description: str = ""


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

    def build_agent(
        self,
        spec: AgentInvocationSpec,
    ) -> tuple[BaseAgent, BaseChatModel]:
        """根据业务配置构造一次性的 DeepAgents 运行实例。"""
        app_id = spec.app_id or (spec.app.id if spec.app else None)
        if app_id is None:
            raise ValueError("Agent 调用缺少 app_id")
        llm = spec.llm or self.language_model_service.load_language_model(
            spec.app_config.get("model_config", {}),
            account_id=spec.account_id,
        )
        tools = list(spec.extra_tools)
        tools.extend(
            self.app_config_service.get_langchain_tools_by_tools_config(
                spec.app_config.get("tools", []),
                account_id=spec.account_id,
            )
        )

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

        runtime = self.agent_runtime_service.prepare(
            account_id=spec.account_id,
            app_id=app_id,
            conversation_id=spec.conversation_id,
            created_by=spec.created_by,
            end_user_id=spec.end_user_id,
            skill_bindings=spec.app_config.get("skills", []),
            subagents=spec.app_config.get("subagents", []),
        )
        try:
            agent = BaseAgent(
                llm=llm,
                checkpointer=runtime.checkpointer,
                backend=runtime.backend,
                store=runtime.store,
                permissions=runtime.permissions,
                skill_sources=runtime.skill_sources,
                memory_sources=runtime.memory_sources,
                subagent_skill_sources=runtime.subagent_skill_sources,
                telemetry=runtime.telemetry,
                runtime_context=runtime.runtime_context,
                agent_config=build_agent_config_from_app_config(
                    user_id=spec.user_id,
                    invoke_from=spec.invoke_from,
                    app_name=spec.app.name if spec.app else spec.app_name,
                    app_description=(
                        spec.app.description if spec.app else spec.app_description
                    ),
                    app_config=spec.app_config,
                    tools=tools,
                    opening_statement=spec.opening_statement,
                ),
            )
        except Exception:
            # BaseAgent normally closes its backend on construction failure.
            # Keep this guard for custom runtime resources that fail before
            # BaseAgent takes ownership.
            try:
                close = getattr(runtime.backend, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
            raise
        return agent, llm

    def stream(
        self,
        spec: AgentInvocationSpec,
    ) -> Generator[AgentThought, None, None]:
        """流式执行 Agent，并在正常结束后统一持久化轨迹。"""
        agent, llm = self.build_agent(spec)

        # 先完成 build_agent，再返回真正的生成器。这样模型/API Key/
        # 工具装配异常会在 Flask 创建响应前抛出，可以返回正常业务错误。
        def generate() -> Generator[AgentThought, None, None]:
            thought_map: dict[str, AgentThought] = {}
            completed = False
            try:
                input_state = {
                    "messages": [
                        llm.convert_to_human_message(
                            spec.query,
                            spec.image_urls,
                        )
                    ],
                }
                for thought in agent.stream(input_state):
                    self._collect_thought(thought_map, thought)
                    yield thought
                completed = True
            finally:
                if completed:
                    self._persist(spec, thought_map.values())

        return generate()

    def invoke(self, spec: AgentInvocationSpec):
        """非流式执行 Agent，并统一持久化轨迹。"""
        agent, llm = self.build_agent(spec)
        result = agent.invoke(
            {
                "messages": [
                    llm.convert_to_human_message(
                        spec.query,
                        spec.image_urls,
                    )
                ],
            }
        )
        self._persist(spec, result.agent_thoughts)
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
    ) -> None:
        self.conversation_service.save_agent_thoughts(
            account_id=spec.account_id,
            app_id=spec.app_id or spec.app.id,
            app_config=dict(spec.app_config),
            conversation_id=spec.conversation_id,
            message_id=spec.message_id,
            agent_thoughts=list(thoughts),
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
