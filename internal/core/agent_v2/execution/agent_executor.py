"""Agent V2 单次 Deep Agents 图执行与资源生命周期。"""

from enum import Enum, auto
from threading import Lock
from typing import Any, Iterator

from deepagents.backends.protocol import BackendProtocol
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.config import merge_configs
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import RunControl

from internal.core.agent_v2.assembly.configuration.langgraph_conversation_config import (
    merge_conversation_config,
)
from internal.core.agent_v2.assembly.context.runtime_resources import (
    AgentRuntimeResources,
)
from internal.core.agent_v2.assembly.observability.run_observer import (
    PlatformAgentRunObserver,
)
from internal.core.agent_v2.backends.composition.composite_backend_factory import (
    close_runtime_backends,
)
from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.exception import FailException
from internal.extension.logging_extension import logger


class _ExecutorState(Enum):
    """表达单次执行器唯一合法的资源生命周期状态。"""

    READY = auto()
    RUNNING = auto()
    FINISHED = auto()
    CLOSED = auto()


class AgentExecutor:
    """持有单次 Deep Agents v3 图运行及其资源生命周期。"""

    def __init__(
        self,
        *,
        graph: CompiledStateGraph,
        runtime: AgentRuntimeResources,
        observer: PlatformAgentRunObserver,
    ) -> None:
        """接管已编译图和当前 Run 的资源生命周期。"""
        self._graph: CompiledStateGraph = graph
        self._backend: BackendProtocol = runtime.backend
        self._runtime_context: AgentRunContext = runtime.runtime_context
        self._observer = observer
        self._run_lock = Lock()
        self._state = _ExecutorState.READY
        self._run_control: RunControl | None = None

    def close(self) -> None:
        """串行关闭非运行态资源；失败后保留为可重试关闭状态。"""
        with self._run_lock:
            if self._state is _ExecutorState.RUNNING:
                raise FailException("智能体仍在运行，不能提前关闭运行时资源")
            if self._state is _ExecutorState.CLOSED:
                return
            # close 一旦被请求，即使部分 Backend 关闭失败也不能再次运行；
            # FINISHED 状态允许调用方稍后重试幂等关闭。
            self._state = _ExecutorState.FINISHED
            close_runtime_backends(self._backend)
            self._state = _ExecutorState.CLOSED

    def request_drain(self, reason: str = "cancel_requested") -> bool:
        """请求当前图在下一个 superstep 边界协作式停止。"""
        with self._run_lock:
            control = self._run_control
            running = self._state is _ExecutorState.RUNNING
        if control is None or not running:
            return False
        control.request_drain(reason)
        return True

    def _finish_run(
        self,
        *,
        preserve_run_error: bool,
    ) -> None:
        """结束执行状态并关闭资源，且不让清理错误覆盖图执行错误。"""
        with self._run_lock:
            self._state = _ExecutorState.FINISHED
            self._run_control = None

        try:
            self.close()
        except Exception:
            if not preserve_run_error:
                raise
            logger.exception("Agent 运行异常后的 Backend 清理失败")

    def stream_events(
        self,
        input: dict[str, Any],
        config: RunnableConfig | None = None,
    ) -> Iterator[dict[str, Any]]:
        """直接输出 Deep Agents v3 事件，并在结束时释放运行资源。"""
        messages = list(input.get("messages", []) or [])
        if not messages:
            try:
                self.close()
            except Exception:
                logger.exception("无效输入导致 Agent 关闭失败")
            raise FailException("智能体输入消息不能为空")
        with self._run_lock:
            if self._state is _ExecutorState.CLOSED:
                raise FailException("智能体运行时资源已经关闭")
            if self._state is not _ExecutorState.READY:
                raise FailException("AgentExecutor 只允许执行一次")
            self._state = _ExecutorState.RUNNING

        try:
            run_control = RunControl()
            with self._run_lock:
                self._run_control = run_control
            effective_config = merge_configs(
                merge_conversation_config(self._runtime_context, config),
                {
                    "run_id": self._observer.run_id,
                    "callbacks": [self._observer],
                    "metadata": {
                        "trace_id": self._observer.trace_id,
                        "run_id": self._observer.trace_id,
                    },
                },
            )
            yield from self._graph.stream_events(
                {"messages": messages},
                config=effective_config,
                context=self._runtime_context,
                version="v3",
                control=run_control,
            )
        except BaseException:
            self._finish_run(
                preserve_run_error=True,
            )
            raise
        else:
            self._finish_run(
                preserve_run_error=False,
            )


__all__ = ["AgentExecutor"]
