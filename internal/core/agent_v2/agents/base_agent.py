from collections.abc import Mapping, Sequence
from threading import Lock, Thread
from typing import Optional, Any, Iterator
from uuid import uuid4

from deepagents.backends.protocol import BackendProtocol
from deepagents.middleware.filesystem import FilesystemPermission
from langchain_core.language_models import BaseChatModel
from langchain_core.load import Serializable
from langchain_core.runnables import Runnable, RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph
from pydantic import ConfigDict, PrivateAttr

from internal.core.agent_v2.agents.execution import (
    GraphExecutionDriver,
    close_runtime_backends,
)
from internal.core.agent_v2.agents.invocation import extract_query_and_images
from internal.core.agent_v2.agents.results import AgentResultAccumulator
from internal.core.agent_v2.agents.models import AgentConfig, AgentResult, AgentThought
from internal.core.agent_v2.runtime.models import AgentRunContext
from internal.core.agent_v2.runtime.factory import build_deep_agent
from internal.core.agent_v2.runtime.prompt import build_app_system_prompt
from internal.core.agent_v2.runtime.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)
from internal.core.agent_v2.runtime.thread_config import merge_thread_config
from internal.exception import FailException
from .agent_queue_manager import AgentQueueManager


_DEEPAGENTS_TOOL_NAMES = frozenset(
    {
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
)


class BaseAgent(Serializable, Runnable):
    """单次 DeepAgents 运行与现有 SSE 队列协议的适配层。"""

    llm: BaseChatModel
    agent_config: AgentConfig
    _agent: CompiledStateGraph = PrivateAttr(None)
    _agent_queue_manager: AgentQueueManager = PrivateAttr(None)
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
        self._checkpointer = checkpointer
        self._backend = backend
        self._store = store
        self._permissions = list(permissions or [])
        self._skill_sources = list(skill_sources or [])
        self._memory_sources = list(memory_sources or [])
        self._subagent_skill_sources = dict(subagent_skill_sources or {})
        self._subagent_tools_by_name = {
            name: list(tools)
            for name, tools in (subagent_tools_by_name or {}).items()
        }
        self._subagent_models_by_name = dict(subagent_models_by_name or {})
        self._telemetry = telemetry or NOOP_AGENT_TELEMETRY
        self._runtime_context = runtime_context
        try:
            self._agent = self._build_agent()
            self._agent_queue_manager = AgentQueueManager(
                user_id=agent_config.user_id,
                invoke_from=agent_config.invoke_from,
            )
        except Exception:
            close_runtime_backends(self._backend)
            raise

    def _build_agent(self) -> CompiledStateGraph:
        """根据 Agent 配置和运行时依赖构造 DeepAgents 图。"""
        return build_deep_agent(
            llm=self.llm,
            tools=self._merge_agent_tools(),
            system_prompt=build_app_system_prompt(
                app_name=self.agent_config.app_name,
                app_description=self.agent_config.app_description,
                preset_prompt=self.agent_config.preset_prompt,
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
            subagents=self.agent_config.subagents,
        )

    def _merge_agent_tools(self) -> list[BaseTool]:
        """合并应用工具并拒绝同名覆盖。"""
        tools = list(self.agent_config.tools)
        names = [tool.name for tool in tools]
        if len(names) != len(set(names)):
            raise FailException("Agent 工具名称冲突，无法安全装配运行时工具")
        reserved = sorted(set(names) & _DEEPAGENTS_TOOL_NAMES)
        if reserved:
            raise FailException(
                "Agent 工具名称与 DeepAgents 内置工具冲突: "
                + ", ".join(reserved)
            )
        return tools

    def close(self) -> None:
        """关闭尚未开始或已经结束的运行时资源。"""
        with self._run_lock:
            if self._run_active:
                raise FailException("智能体仍在运行，不能提前关闭运行时资源")
            if self._resources_closed:
                return
            self._resources_closed = True
        close_runtime_backends(self._backend)

    def __enter__(self) -> "BaseAgent":
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
                raise FailException("BaseAgent 只允许执行一次")
            self._run_started = True
            self._run_active = True

    def _close_after_run(self) -> None:
        """在图执行结束后关闭资源，并允许清理异常回到事件层。"""
        with self._run_lock:
            if self._resources_closed:
                return
            self._resources_closed = True
            self._run_active = False
        close_runtime_backends(self._backend)

    def invoke(
        self,
        input: dict[str, Any],
        config: Optional[RunnableConfig] = None,
        **kwargs: Any,
    ) -> AgentResult:
        """把事件流聚合为非流式响应。"""
        try:
            query, image_urls = extract_query_and_images(input)
        except Exception:
            self.close()
            raise
        accumulator = AgentResultAccumulator(query=query, image_urls=image_urls)
        stream = self.stream(input, config, **kwargs)
        try:
            for agent_thought in stream:
                accumulator.add(agent_thought)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
        return accumulator.build()

    def stream(
        self,
        input: dict[str, Any],
        config: Optional[RunnableConfig] = None,
        **kwargs: Any,
    ) -> Iterator[AgentThought]:
        """流式输出，并保持现有 SSE queue/心跳/停止协议。"""
        if not self._agent:
            raise FailException("智能体未成功构建，请核实后尝试")

        messages = list(input.get("messages", []) or [])
        if not messages:
            self.close()
            raise FailException("智能体输入消息不能为空")
        self._begin_run()
        graph_input = {"messages": messages}
        task_id = uuid4()
        effective_config = self._merge_thread_config(config)

        # 后台线程执行图，当前线程继续处理队列心跳和停止标记。
        self._agent_queue_manager.queue(task_id)
        thread = Thread(
            target=self._run_stream_in_thread,
            args=(graph_input, task_id, effective_config),
            kwargs=kwargs,
        )
        thread.daemon = True
        try:
            thread.start()
        except Exception:
            self._agent_queue_manager.request_stop(task_id)
            self._close_after_run()
            raise

        completed = False
        try:
            yield from self._agent_queue_manager.listen(task_id)
            completed = True
        finally:
            if not completed:
                self._agent_queue_manager.request_stop(task_id)
            if thread.is_alive():
                thread.join(timeout=5)
                if thread.is_alive():
                    self._telemetry.emit(
                        "agent.run_thread_join_timeout",
                        attributes={"task_id": str(task_id)},
                    )

    def _run_stream_in_thread(
        self,
        input: dict[str, Any],
        task_id,
        config: Optional[RunnableConfig],
        **kwargs: Any,
    ) -> None:
        """把图执行委托给单次运行驱动器。"""
        GraphExecutionDriver(
            graph=self._agent,
            publisher=self._agent_queue_manager.publish,
            runtime_context=self._runtime_context,
            telemetry=self._telemetry,
            cancelled=lambda: self._agent_queue_manager.is_stopped(task_id),
            on_cleanup=self._close_after_run,
        ).run(
            input_state=input,
            task_id=task_id,
            config=config,
            stream_options=kwargs,
        )

    def _merge_thread_config(
        self,
        config: Optional[RunnableConfig],
    ) -> Optional[RunnableConfig]:
        """通过运行时方法合并稳定线程标识。"""
        return merge_thread_config(self._runtime_context, config)


__all__ = ["BaseAgent"]
