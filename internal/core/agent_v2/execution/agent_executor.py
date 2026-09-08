"""Agent V2 单次 Deep Agents 图执行与资源生命周期。"""

from collections.abc import Callable, Generator, Mapping
from enum import Enum, auto
from threading import Event, Lock
from typing import Any, Literal

from deepagents.backends.protocol import BackendProtocol
from langchain_core.runnables import RunnableConfig
from langchain_core.messages import BaseMessage
from langchain_core.runnables.config import merge_configs
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import RunControl
from langgraph.errors import GraphDrained

from internal.core.agent_v2.assembly.configuration.langgraph_conversation_config import (
    merge_conversation_config,
)
from internal.core.agent_v2.entity.runtime_resources import (
    AgentRuntimeResources,
)
from internal.core.agent_v2.observability.run_observer import (
    PlatformAgentRunObserver,
)
from internal.core.agent_v2.backends.composition.composite_backend_factory import (
    close_runtime_backends,
)
from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.entity.execution_result import AgentExecutionResult
from internal.core.agent_v2.entity.agent_event import AgentEvent
from internal.core.agent_v2.execution.message_parser import message_content_to_text
from internal.core.agent_v2.middleware.review_middleware import ReviewMiddleware
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
        review_config: Mapping[str, Any] | None = None,
    ) -> None:
        """接管已编译图和当前 Run 的资源生命周期。"""
        self._graph: CompiledStateGraph = graph
        self._backend: BackendProtocol = runtime.backend
        self._runtime_context: AgentRunContext = runtime.runtime_context
        self._observer = observer
        self._review = ReviewMiddleware(review_config=dict(review_config or {}))
        self._run_lock = Lock()
        self._state = _ExecutorState.READY
        self._run_control: RunControl | None = None

    def execute(
        self,
        input: dict[str, Any],
        *,
        on_event: Callable[[AgentEvent], None],
        cancel_requested: Event,
    ) -> AgentExecutionResult:
        """消费图事件、处理取消并交付结果；事件接收方只负责传输和存储。"""
        state: dict[str, Any] = {}
        error: Exception | None = None
        cancelled = cancel_requested.is_set()
        raw_stream = self.stream_events(input)
        reviewed_events: list[AgentEvent] = []
        try:
            # 1.取消后请求图协作退出，继续消费以完成图内部清理。
            if not cancelled:
                drain_requested = False
                for raw_event in raw_stream:
                    event = AgentEvent.from_raw(raw_event)
                    if (
                        event.channel == "values"
                        and not event.namespace
                        and isinstance(event.data, Mapping)
                    ):
                        state = dict(event.data)
                    if cancel_requested.is_set():
                        cancelled = True
                        if not drain_requested:
                            drain_requested = self.request_drain()
                            if not drain_requested:
                                break
                        continue

                    # 2.取消后的根状态仍用于保存部分结果，但不再向前端输出增量。
                    if self._review.output_enabled:
                        # 原生消息增量和 values 都可能携带未审核文本，整轮通过后再公开。
                        if event.channel == "values" and isinstance(event.data, Mapping):
                            for message in reversed(event.data.get("messages", []) or []):
                                if isinstance(message, BaseMessage):
                                    role, content = message.type, message.content
                                elif isinstance(message, Mapping):
                                    role = message.get("type") or message.get("role")
                                    content = message.get("content")
                                else:
                                    continue
                                if role in {"human", "user"}:
                                    break
                                if role in {"ai", "assistant"}:
                                    self._review.check_output(content)
                        reviewed_events.append(event)
                    else:
                        on_event(event)
                if not cancelled and not cancel_requested.is_set():
                    for event in reviewed_events:
                        if cancel_requested.is_set():
                            cancelled = True
                            break
                        on_event(event)
        except GraphDrained as exc:
            if exc.reason == "cancel_requested" or cancel_requested.is_set():
                cancelled = True
            else:
                error = exc
        except Exception as exc:
            error = exc
        finally:
            # 3.回调异常或执行前取消时也必须关闭流和运行时，保留最初的错误。
            try:
                raw_stream.close()
            except Exception as exc:
                if error is None:
                    error = exc
                else:
                    logger.exception("Agent 事件流关闭失败，保留原始执行错误")
            finally:
                try:
                    self.close()
                except Exception as exc:
                    if error is None:
                        error = exc
                    else:
                        logger.exception("Agent 执行失败后的资源关闭失败")

        # 4.从根图最终状态提取回答，并结束观测与用量统计。
        if self._review.output_enabled and (error is not None or cancelled or cancel_requested.is_set()):
            # 未通过交付审核的回答也不能经业务历史或取消结果公开。
            state = {key: value for key, value in state.items() if key != "messages"}
        answer = ""
        messages = state.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                if isinstance(message, BaseMessage):
                    role, content = message.type, message.content
                elif isinstance(message, Mapping):
                    role = message.get("type") or message.get("role")
                    content = message.get("content")
                else:
                    continue
                if role in {"human", "user"}:
                    break
                if role not in {"ai", "assistant"}:
                    continue
                answer = message_content_to_text(content)
                if answer:
                    break
        status: Literal["completed", "cancelled", "failed"] = "completed"
        if error is not None:
            status = "failed"
        elif cancelled or cancel_requested.is_set():
            status = "cancelled"
        self._observer.finish(status, error=error)
        return AgentExecutionResult(
            status=status,
            answer=answer,
            state=state,
            usage=self._observer.build_run_usage(),
            observability=self._observer.snapshot(),
            error=error,
        )

    def close(self) -> None:
        """串行关闭非运行态资源；失败后保留为可重试关闭状态。"""
        with self._run_lock:
            if self._state is _ExecutorState.RUNNING:
                raise RuntimeError("智能体仍在运行，不能提前关闭运行时资源")
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
    ) -> Generator[dict[str, Any], None, None]:
        """直接输出 Deep Agents v3 事件，并在结束时释放运行资源。"""
        messages = list(input.get("messages", []) or [])
        if not messages:
            try:
                self.close()
            except Exception:
                logger.exception("无效输入导致 Agent 关闭失败")
            raise ValueError("智能体输入消息不能为空")
        with self._run_lock:
            if self._state is _ExecutorState.CLOSED:
                raise RuntimeError("智能体运行时资源已经关闭")
            if self._state is not _ExecutorState.READY:
                raise RuntimeError("AgentExecutor 只允许执行一次")
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
