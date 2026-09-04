"""Agent V2 单次 Deep Agents 图执行与资源生命周期。"""

import logging
from collections.abc import Mapping, Sequence
from threading import Lock
from typing import Any, Iterator, Optional
from uuid import UUID, uuid4

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.load import Serializable
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import RunControl
from pydantic import ConfigDict, PrivateAttr

from internal.core.agent_v2.assembly.configuration.agent_system_prompt import (
    build_app_system_prompt,
)
from internal.core.agent_v2.assembly.configuration.langgraph_thread_config import (
    merge_thread_config,
)
from internal.core.agent_v2.assembly.graph.deep_agent_factory import build_deep_agent
from internal.core.agent_v2.assembly.graph.subagent_spec_adapter import (
    validate_agent_tools,
)
from internal.core.agent_v2.assembly.observability.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)
from internal.core.agent_v2.assembly.observability.token_usage import (
    build_framework_run_usage,
)
from internal.core.agent_v2.backends.composition.composite_backend_factory import (
    close_runtime_backends,
)
from internal.core.agent_v2.entity.agent_config import AgentConfig
from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.exception import FailException

_logger = logging.getLogger(__name__)


class AgentExecutor(Serializable):
    """持有单次 Deep Agents v3 图运行及其资源生命周期。"""

    llm: BaseChatModel
    agent_config: AgentConfig
    _agent: CompiledStateGraph = PrivateAttr(None)
    _backend: BackendProtocol | None = PrivateAttr(None)
    _runtime_context: AgentRunContext = PrivateAttr()
    _run_lock: Lock = PrivateAttr(default_factory=Lock)
    _run_started: bool = PrivateAttr(False)
    _run_active: bool = PrivateAttr(False)
    _run_control: RunControl | None = PrivateAttr(None)
    _resources_closed: bool = PrivateAttr(False)
    _last_run_usage: dict[str, Any] | None = PrivateAttr(default=None)

    model_config = ConfigDict(
        # 允许字段接收框架运行时对象。
        arbitrary_types_allowed=True
    )

    def __init__(
        self,
        llm: BaseChatModel,
        agent_config: AgentConfig,
        runtime_context: AgentRunContext,
        checkpointer: BaseCheckpointSaver | bool | None = None,
        backend: BackendProtocol | None = None,
        store: Any | None = None,
        permissions: list[FilesystemPermission] | None = None,
        skill_sources: list[str] | None = None,
        memory_sources: list[str] | None = None,
        subagent_skill_sources: dict[str, list[str]] | None = None,
        subagent_tools_by_name: Mapping[str, Sequence[BaseTool]] | None = None,
        subagent_models_by_name: Mapping[str, BaseChatModel | str] | None = None,
        telemetry: AgentTelemetry | None = None,
    ):
        """初始化 Agent 并构造 DeepAgents 图。"""
        super().__init__(llm=llm, agent_config=agent_config)
        # 只保存运行期需要关闭或读取的资源。
        self._backend = backend
        self._runtime_context = runtime_context

        # 构图所需的可变配置只在构造期间使用，避免挂在执行器实例上。
        permissions = list(permissions or [])
        skill_sources = list(skill_sources or [])
        memory_sources = list(memory_sources or [])
        subagent_skill_sources = dict(subagent_skill_sources or {})
        subagent_tools_by_name = {
            name: list(tools) for name, tools in (subagent_tools_by_name or {}).items()
        }
        subagent_models_by_name = dict(subagent_models_by_name or {})
        telemetry = telemetry or NOOP_AGENT_TELEMETRY
        try:
            agent_tools = validate_agent_tools(self.agent_config.tools)
            system_prompt = build_app_system_prompt(
                app_name=self.agent_config.app_name,
                app_description=self.agent_config.app_description,
                preset_prompt=self.agent_config.preset_prompt,
                memory_enabled=self.agent_config.memory_enabled,
                selected_skills=self.agent_config.selected_skills,
            )
            subagents = [
                item.model_dump(mode="json", exclude_none=True)
                for item in self.agent_config.subagents
            ]
            self._agent = build_deep_agent(
                llm=self.llm,
                tools=agent_tools,
                system_prompt=system_prompt,
                review_config=self.agent_config.review_config,
                max_model_calls=self.agent_config.max_model_calls,
                max_tool_calls=self.agent_config.max_tool_calls,
                checkpointer=checkpointer,
                backend=self._backend,
                store=store,
                permissions=permissions,
                skill_sources=skill_sources,
                memory_sources=memory_sources,
                subagent_skill_sources=subagent_skill_sources,
                subagent_tools_by_name=subagent_tools_by_name,
                subagent_models_by_name=subagent_models_by_name,
                subagents=subagents,
                telemetry=telemetry,
            )
        except Exception:
            self._close_backend_quietly("关闭 Agent 图构建阶段 Backend 失败")
            raise

    def close(self) -> None:
        """关闭尚未开始或已经结束的运行时资源。"""
        with self._run_lock:
            if self._run_active:
                raise FailException("智能体仍在运行，不能提前关闭运行时资源")
            if self._resources_closed:
                return
            self._resources_closed = True
        self._release_backend()

    def _begin_run(self) -> None:
        """把 Agent 标记为单次运行，拒绝重复或并发调用。"""
        with self._run_lock:
            if self._resources_closed:
                raise FailException("智能体运行时资源已经关闭")
            if self._run_started:
                raise FailException("AgentExecutor 只允许执行一次")
            self._run_started = True
            self._run_active = True
            self._last_run_usage = None

    @property
    def last_run_usage(self) -> dict[str, Any] | None:
        """返回最近一次运行的聚合用量。"""
        if self._last_run_usage is None:
            return None
        return dict(self._last_run_usage)

    def _close_after_run(self) -> None:
        """在图执行结束后关闭资源，并允许清理异常回到事件层。"""
        with self._run_lock:
            if self._resources_closed:
                return
            self._resources_closed = True
            self._run_active = False
            self._run_control = None
        self._release_backend()

    def request_drain(self, reason: str = "cancel_requested") -> bool:
        """请求当前图在下一个 superstep 边界协作式停止。"""
        with self._run_lock:
            control = self._run_control
            active = self._run_active
        if control is None or not active:
            return False
        control.request_drain(reason)
        return True

    def _release_backend(self) -> None:
        """释放运行时 Backend；失败时恢复可重试状态。"""
        try:
            close_runtime_backends(self._backend)
        except Exception:
            with self._run_lock:
                self._resources_closed = False
            raise

    def _close_backend_quietly(self, message: str) -> None:
        """在已有异常上下文中尽力释放 Backend，不覆盖原异常。"""
        try:
            close_runtime_backends(self._backend)
        except Exception:
            _logger.exception(message)

    def _close_invalid_input(self) -> None:
        """输入无效时关闭未开始运行的执行器。"""
        try:
            self.close()
        except Exception:
            _logger.exception("无效输入导致 Agent 关闭失败")

    def _build_stream_config(
        self,
        config: Optional[RunnableConfig],
        usage_callback: UsageMetadataCallbackHandler,
    ) -> RunnableConfig:
        """合并线程配置并追加本次运行的用量回调。"""
        effective_config: RunnableConfig = dict(
            merge_thread_config(self._runtime_context, config) or {}
        )
        existing_callbacks = effective_config.get("callbacks")
        if existing_callbacks is None:
            callbacks = []
        elif isinstance(existing_callbacks, list):
            callbacks = list(existing_callbacks)
        else:
            callbacks = [existing_callbacks]
        callbacks.append(usage_callback)
        effective_config["callbacks"] = callbacks
        return effective_config

    def _finish_run(
        self,
        usage_callback: UsageMetadataCallbackHandler,
        usage_id: UUID,
        trace_id: UUID,
    ) -> None:
        """记录运行用量并释放资源，确保清理始终执行。"""
        try:
            run_usage = build_framework_run_usage(
                usage_callback.usage_metadata,
                usage_id=usage_id,
                trace_id=trace_id,
            )
            if run_usage is not None:
                self._last_run_usage = dict(run_usage)
        finally:
            self._close_after_run()

    def stream_events(
        self,
        input: dict[str, Any],
        config: Optional[RunnableConfig] = None,
        **kwargs: Any,
    ) -> Iterator[dict[str, Any]]:
        """直接输出 Deep Agents v3 事件，并在结束时释放运行资源。"""
        if not self._agent:
            raise FailException("智能体未成功构建，请核实后尝试")
        messages = list(input.get("messages", []) or [])
        if not messages:
            self._close_invalid_input()
            raise FailException("智能体输入消息不能为空")
        self._begin_run()
        usage_callback = UsageMetadataCallbackHandler()
        usage_id = uuid4()
        usage_trace_id = uuid4()
        try:
            run_control = RunControl()
            with self._run_lock:
                self._run_control = run_control
            stream_kwargs = {**kwargs, "control": run_control}
            effective_config = self._build_stream_config(config, usage_callback)
            yield from self._agent.stream_events(
                {"messages": messages},
                config=effective_config,
                context=self._runtime_context,
                version="v3",
                **stream_kwargs,
            )
        finally:
            self._finish_run(usage_callback, usage_id, usage_trace_id)


__all__ = ["AgentExecutor"]
