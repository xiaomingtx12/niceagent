"""Agent V2 单次 Deep Agents 图执行与资源生命周期。"""

from collections.abc import Callable, Generator, Mapping
from copy import deepcopy
from enum import Enum, auto
from threading import Event, Lock
from typing import Any, Literal

from langchain_core.messages import BaseMessage
from langchain_core.runnables.config import merge_configs
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import RunControl
from langgraph.errors import GraphDrained
from langgraph.types import Command
from internal.core.agent_v2.execution.user_input_interrupt_tracker import UserInputInterruptTracker
from internal.core.agent_v2.execution.summary_progress import SummaryProgressCallback

from internal.core.agent_v2.execution.runtime_resources import (
    AgentRuntimeResources,
)
from internal.core.agent_v2.observability.run_observer import (
    PlatformAgentRunObserver,
)
from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.entity.execution_result import AgentExecutionResult
from internal.core.agent_v2.entity.agent_event import AgentEvent
from internal.core.agent_v2.execution.message_parser import final_answer
from internal.core.agent_v2.execution.event_delivery import AgentEventDelivery
from internal.core.agent_v2.execution.event_normalizer import json_value
from internal.core.agent_v2.middleware.review_middleware import ReviewMiddleware
from internal.extension.logging_extension import logger


class _ExecutorState(Enum):
    """表达单次执行器唯一合法的资源生命周期状态。"""

    READY = auto()
    RUNNING = auto()
    FINISHED = auto()
    CLOSED = auto()
    CLOSE_FAILED = auto()


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
        self._resources = runtime
        self._runtime_context: AgentRunContext = runtime.runtime_context
        self._observer = observer
        self._review = ReviewMiddleware(review_config=dict(review_config or {}))
        self._run_lock = Lock()
        self._state = _ExecutorState.READY
        self._run_control: RunControl | None = None
        self._cancel_requested: Event | None = None
        self._interrupts: tuple[dict[str, Any], ...] = ()

    def execute(
        self,
        message: BaseMessage | None = None,
        *,
        resume: Mapping[str, Any] | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
        cancel_requested: Event | None = None,
    ) -> AgentExecutionResult:
        """消息与恢复答案二选一；统一交付结果并关闭资源，事件订阅和取消信号可选。"""
        if cancel_requested is None:
            cancel_requested = Event()
        state: dict[str, Any] = {}
        error: Exception | None = None
        cancelled = cancel_requested.is_set()
        raw_stream = self._stream_events(message, resume=resume, cancel_requested=cancel_requested)
        delivery = AgentEventDelivery(self._review, on_event)
        try:
            if not cancelled:
                cancelled = self._consume_stream(raw_stream, state, delivery, cancel_requested)
                if not cancelled and not cancel_requested.is_set():
                    delivery.flush(cancel_requested)
        except GraphDrained as exc:
            if exc.reason == "cancel_requested" or cancel_requested.is_set():
                cancelled = True
            else:
                error = exc
        except Exception as exc:
            error = exc
        finally:
            delivery.discard()
            # 回调异常或执行前取消时也必须关闭流和运行时，保留最初的错误。
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

        try:
            delivery.finish()
        except Exception as exc:
            if error is None:
                error = exc
            else:
                logger.exception("结束 Agent 消息流失败，保留原始执行错误")
        return self._build_result(
            state=state,
            error=error,
            cancelled=cancelled or cancel_requested.is_set(),
        )

    def close(self) -> None:
        """串行清理非运行态资源；失败是终态，不能重跑已释放会话的同步流程。"""
        with self._run_lock:
            if self._state is _ExecutorState.RUNNING:
                raise RuntimeError("智能体仍在运行，不能提前关闭运行时资源")
            if self._state is _ExecutorState.CLOSED:
                return
            self._state = _ExecutorState.CLOSE_FAILED
            self._resources.close()
            self._state = _ExecutorState.CLOSED

    def request_cancel(self) -> bool:
        """同时停止公开事件并请求图排空；未启动或已结束时返回 False。"""
        with self._run_lock:
            control = self._run_control
            if (self._state is not _ExecutorState.RUNNING
                    or control is None or self._cancel_requested is None):
                return False
            self._cancel_requested.set()
        control.request_drain("cancel_requested")
        return True

    def _stream_events(
        self,
        message: BaseMessage | None,
        *,
        resume: Mapping[str, Any] | None,
        cancel_requested: Event,
    ) -> Generator[dict[str, Any], None, None]:
        """将业务输入转换为框架命令；原生事件流和会话配置仅在 Core 内使用。"""
        if (message is None) == (resume is None):
            raise ValueError("必须且只能提供输入消息或恢复答案")
        if message is not None and not isinstance(message, BaseMessage):
            raise ValueError("输入消息必须是 BaseMessage")
        if resume is not None and not isinstance(resume, Mapping):
            raise ValueError("恢复答案必须是中断标识到答案的映射")
        with self._run_lock:
            if self._state is _ExecutorState.CLOSED:
                raise RuntimeError("智能体运行时资源已经关闭")
            if self._state is not _ExecutorState.READY:
                raise RuntimeError("AgentExecutor 只允许执行一次")
            self._state = _ExecutorState.RUNNING

        summary_progress = SummaryProgressCallback()
        try:
            run_control = RunControl()
            interrupt_tracker = UserInputInterruptTracker()
            with self._run_lock:
                self._run_control = run_control
                self._cancel_requested = cancel_requested
            effective_config = merge_configs(
                self._runtime_context.conversation.to_config(),
                {
                    "run_id": self._observer.run_id,
                    "callbacks": [self._observer, interrupt_tracker, summary_progress],
                    "metadata": {
                        "trace_id": self._observer.trace_id,
                        "run_id": self._observer.trace_id,
                    },
                },
            )
            stream = self._graph.stream_events(
                Command(resume=dict(resume)) if resume is not None else {"messages": [message]},
                config=effective_config,
                context=self._runtime_context,
                version="v3",
                control=run_control,
            )
            try:
                for raw in stream:
                    data = raw.get("params", {}).get("data")
                    if (raw.get("method") == "tools" and isinstance(data, Mapping)
                            and data.get("event") == "tool-error"
                            and interrupt_tracker.is_interrupted(data.get("tool_call_id"))):
                        continue
                    yield raw
            finally:
                stream.abort()
            if stream.interrupted:
                self._interrupts = tuple(
                    {"interrupt_id": item.id, "namespace": [], "payload": item.value}
                    for item in stream.interrupts
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
        finally:
            summary_progress.close()

    def _consume_stream(
        self,
        raw_stream: Generator[dict[str, Any], None, None],
        state: dict[str, Any],
        delivery: AgentEventDelivery,
        cancel_requested: Event,
    ) -> bool:
        """取消后协作式排空图；继续收集根状态，但停止公开后续事件。"""
        cancelled = False
        drain_requested = False
        for raw_event in raw_stream:
            params = raw_event.get("params", {})
            data = params.get("data")
            if raw_event.get("method") == "values" and not params.get("namespace") and isinstance(data, Mapping):
                state.clear()
                state.update(data)
            if cancel_requested.is_set():
                cancelled = True
                if not drain_requested:
                    drain_requested = self.request_cancel()
                    if not drain_requested:
                        break
                continue
            delivery.accept(raw_event)
        return cancelled

    def _build_result(
        self,
        *,
        state: dict[str, Any],
        error: Exception | None,
        cancelled: bool,
    ) -> AgentExecutionResult:
        """统一完成状态、回答与观测，阻止未通过交付审核的消息进入业务历史。"""
        if self._review.output_enabled and (error is not None or cancelled):
            state = {key: value for key, value in state.items() if key != "messages"}
        messages = state.get("messages")
        answer = final_answer(messages) if isinstance(messages, list) else ""
        status: Literal["completed", "cancelled", "failed", "waiting_input"] = "completed"
        if error is not None:
            status = "failed"
        elif cancelled:
            status = "cancelled"
        elif self._interrupts:
            status = "waiting_input"
        self._observer.finish(status, error=error)
        snapshot = self._observer.snapshot()
        return AgentExecutionResult(
            status=status,
            interrupts=self._interrupts if status == "waiting_input" else (),
            answer=answer,
            state=json_value(state),
            usage=deepcopy(snapshot["usage"]),
            observability=snapshot,
            error=error,
        )

    def _finish_run(
        self,
        *,
        preserve_run_error: bool,
    ) -> None:
        """结束执行状态并关闭资源，且不让清理错误覆盖图执行错误。"""
        with self._run_lock:
            self._state = _ExecutorState.FINISHED
            self._run_control = None
            self._cancel_requested = None

        try:
            self.close()
        except Exception:
            if not preserve_run_error:
                raise
            logger.exception("Agent 运行异常后的 Backend 清理失败")


__all__ = ["AgentExecutor"]
