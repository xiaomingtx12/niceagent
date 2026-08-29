from collections.abc import Mapping, Sequence
from threading import Lock
from typing import Optional, Any, Iterator
from uuid import uuid4

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.load import Serializable
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from pydantic import ConfigDict, PrivateAttr

from internal.core.agent_v2.execution.run_resource_manager import (
    append_usage_callback,
    close_runtime_backends,
)
from internal.core.agent_v2.execution.tool_validator import validate_agent_tools
from internal.core.agent_v2.assembly.graph.deep_agent_factory import build_deep_agent
from internal.core.agent_v2.assembly.configuration.agent_system_prompt import (
    build_app_system_prompt,
)
from internal.core.agent_v2.assembly.observability.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)
from internal.core.agent_v2.assembly.configuration.langgraph_thread_config import (
    merge_thread_config,
)
from internal.core.agent_v2.assembly.observability.token_usage import (
    build_framework_run_usage,
)
from internal.core.agent_v2.entity.agent_config import AgentConfig
from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.exception import FailException


class AgentExecutor(Serializable):
    """持有单次 Deep Agents v3 图运行及其资源生命周期。"""

    llm: BaseChatModel
    agent_config: AgentConfig
    _agent: CompiledStateGraph = PrivateAttr(None)
    _checkpointer: BaseCheckpointSaver | bool | None = PrivateAttr(None)
    _backend: BackendProtocol | None = PrivateAttr(None)
    _store: Any | None = PrivateAttr(None)
    _skill_sources: list[str] = PrivateAttr(default_factory=list)
    _memory_sources: list[str] = PrivateAttr(default_factory=list)
    _subagent_skill_sources: dict[str, list[str]] = PrivateAttr(default_factory=dict)
    _subagent_tools_by_name: dict[str, list[BaseTool]] = PrivateAttr(
        default_factory=dict
    )
    _subagent_models_by_name: dict[str, BaseChatModel | str] = PrivateAttr(
        default_factory=dict
    )
    _permissions: list[FilesystemPermission] = PrivateAttr(default_factory=list)
    _telemetry: AgentTelemetry = PrivateAttr(default=NOOP_AGENT_TELEMETRY)
    _runtime_context: AgentRunContext = PrivateAttr()
    _run_lock: Lock = PrivateAttr(default_factory=Lock)
    _run_started: bool = PrivateAttr(False)
    _run_active: bool = PrivateAttr(False)
    _resources_closed: bool = PrivateAttr(False)
    _last_run_usage: dict[str, Any] | None = PrivateAttr(default=None)

    model_config = ConfigDict(
        # 允许字段接收框架运行时对象。
        arbitrary_types_allowed=True
    )

    name: str = "base_agent"

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
        *args,
        **kwargs,
    ):
        """初始化 Agent 并构造 DeepAgents 图。"""
        super().__init__(*args, llm=llm, agent_config=agent_config, **kwargs)
        # 1.保存本次运行需要关闭的底层资源。
        self._checkpointer = checkpointer
        self._backend = backend
        self._store = store
        self._permissions = list(permissions or [])
        # 2.复制外部传入的可变配置，避免运行中被调用方修改。
        self._skill_sources = list(skill_sources or [])
        self._memory_sources = list(memory_sources or [])
        self._subagent_skill_sources = dict(subagent_skill_sources or {})
        self._subagent_tools_by_name = {
            name: list(tools) for name, tools in (subagent_tools_by_name or {}).items()
        }
        self._subagent_models_by_name = dict(subagent_models_by_name or {})
        self._telemetry = telemetry or NOOP_AGENT_TELEMETRY
        self._runtime_context = runtime_context
        try:
            # 3.根据业务配置和运行时资源构建 DeepAgents 图。
            self._agent = self._build_agent()
        except Exception:
            close_runtime_backends(self._backend)
            raise

    def _build_agent(self) -> CompiledStateGraph:
        """根据 Agent 配置和运行时依赖构造 DeepAgents 图。"""
        # 1.合并并校验应用工具，避免覆盖 DeepAgents 内置能力。
        # 2.生成应用身份提示词，并注入本次运行的限制与持久化依赖。
        return build_deep_agent(
            llm=self.llm,
            tools=validate_agent_tools(self.agent_config.tools),
            system_prompt=build_app_system_prompt(
                app_name=self.agent_config.app_name,
                app_description=self.agent_config.app_description,
                preset_prompt=self.agent_config.preset_prompt,
                memory_enabled=self.agent_config.memory_enabled,
            ),
            review_config=self.agent_config.review_config,
            max_model_calls=self.agent_config.max_model_calls,
            max_tool_calls=self.agent_config.max_tool_calls,
            checkpointer=self._checkpointer,
            backend=self._backend,
            store=self._store,
            permissions=self._permissions,
            skill_sources=self._skill_sources,
            memory_sources=self._memory_sources,
            subagent_skill_sources=self._subagent_skill_sources,
            subagent_tools_by_name=self._subagent_tools_by_name,
            subagent_models_by_name=self._subagent_models_by_name,
            subagents=[
                item.model_dump(mode="json", exclude_none=True)
                for item in self.agent_config.subagents
            ],
            telemetry=self._telemetry,
        )

    def close(self) -> None:
        """关闭尚未开始或已经结束的运行时资源。"""
        # 1.在锁内确认当前没有正在执行的调用。
        with self._run_lock:
            if self._run_active:
                raise FailException("智能体仍在运行，不能提前关闭运行时资源")
            if self._resources_closed:
                return
            # 2.先标记资源已关闭，避免重复释放。
            self._resources_closed = True
        # 3.释放复合 Backend 及其下挂载的运行时资源。
        close_runtime_backends(self._backend)

    def __enter__(self) -> "AgentExecutor":
        """支持调用方用上下文管理器托管未执行实例。"""
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        """退出上下文时释放运行时资源。"""
        self.close()

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

    def _record_run_usage(self, usage: dict[str, Any]) -> None:
        """保存当前 v3 图运行生成的最终 Run 级用量。"""
        self._last_run_usage = dict(usage)

    def _close_after_run(self) -> None:
        """在图执行结束后关闭资源，并允许清理异常回到事件层。"""
        with self._run_lock:
            if self._resources_closed:
                return
            self._resources_closed = True
            self._run_active = False
        close_runtime_backends(self._backend)

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
            self.close()
            raise FailException("智能体输入消息不能为空")
        self._begin_run()
        effective_config = self._merge_thread_config(config)
        usage_callback = UsageMetadataCallbackHandler()
        effective_config = append_usage_callback(effective_config, usage_callback)
        usage_id = uuid4()
        usage_trace_id = uuid4()
        try:
            yield from self._agent.stream_events(
                {"messages": messages},
                config=effective_config,
                context=self._runtime_context,
                version="v3",
                **kwargs,
            )
        finally:
            try:
                run_usage = build_framework_run_usage(
                    usage_callback.usage_metadata,
                    usage_id=usage_id,
                    trace_id=usage_trace_id,
                )
                if run_usage is not None:
                    self._record_run_usage(run_usage)
            finally:
                self._close_after_run()

    def _merge_thread_config(
        self,
        config: Optional[RunnableConfig],
    ) -> Optional[RunnableConfig]:
        """通过运行时方法合并稳定线程标识。"""
        return merge_thread_config(self._runtime_context, config)


__all__ = ["AgentExecutor"]
