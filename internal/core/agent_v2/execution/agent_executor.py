"""Agent V2 单次 Deep Agents 图执行与资源生命周期。

公开方法 ``execute`` / ``close`` / ``request_cancel`` 由请求线程和取消监控线程调用；
私有方法是同一公开方法的执行步骤，全部内联在本文件内。
"""

from collections.abc import Callable, Generator, Mapping
from enum import Enum, auto
from threading import Event, Lock
from typing import Any, Literal

from langchain_core.messages import BaseMessage
from langchain_core.runnables.config import merge_configs
from langgraph.errors import GraphDrained
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import RunControl
from langgraph.types import Command

from internal.core.agent_v2.entity.agent_event import AgentEvent
from internal.core.agent_v2.entity.execution_result import AgentExecutionResult
from internal.core.agent_v2.execution.event_delivery import AgentEventDelivery
from internal.core.agent_v2.execution.message_parser import final_answer, json_value
from internal.core.agent_v2.execution.runtime_resources import AgentRuntimeResources
from internal.core.agent_v2.execution.summary_progress import SummaryProgressCallback
from internal.core.agent_v2.execution.user_input_interrupt_tracker import UserInputInterruptTracker
from internal.core.agent_v2.observability.run_observer import PlatformAgentRunObserver
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
        self._resources = runtime
        self._observer = observer
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
        # 1.准备交付容器与初始状态；调用前已取消则直接走清理路径。
        if cancel_requested is None:
            cancel_requested = Event()
        state: dict[str, Any] = {}
        error: Exception | None = None
        cancelled = cancel_requested.is_set()
        delivery = AgentEventDelivery(on_event)
        try:
            # 2.消耗框架事件流；取消后协作排空，未取消时事件随到随交付。
            raw_stream = self._stream_events(message, resume=resume, cancel_requested=cancel_requested)
            try:
                if not cancelled:
                    cancelled = self._consume_stream(raw_stream, state, delivery, cancel_requested)
            except GraphDrained as exc:
                # 3.取消排空是正常退出，仅当原因不明时才视作执行错误。
                if exc.reason == "cancel_requested" or cancel_requested.is_set():
                    cancelled = True
                else:
                    error = exc
            except Exception as exc:
                error = exc
            finally:
                # 4.清理顺序固定：关事件流、关运行资源；首个错误保留为执行错误。
                error = _close_safely("Agent 事件流关闭", raw_stream.close, error)
                error = _close_safely("Agent 运行资源关闭", self.close, error)
        except BaseException:
            # raw_stream 创建前的输入校验或状态守卫异常，仍要走同一清理。
            error = _close_safely("Agent 运行资源关闭", self.close, error)
            raise
        # 5.图正常或异常退出后收尾已交付文本，再统一汇总结果。
        error = _close_safely("Agent 消息流收尾", delivery.finish, error)
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
        """将业务输入转换为框架命令，产出原始 v3 事件流并登记取消控制柄。"""
        # 1.输入必须且只能是消息或恢复答案二者之一。
        if (message is None) == (resume is None):
            raise ValueError("必须且只能提供输入消息或恢复答案")
        if message is not None and not isinstance(message, BaseMessage):
            raise ValueError("输入消息必须是 BaseMessage")
        if resume is not None and not isinstance(resume, Mapping):
            raise ValueError("恢复答案必须是中断标识到答案的映射")
        # 2.创建回调与控制柄，同一把锁内完成状态翻转和登记，
        # 取消请求从这一刻起就能命中排空。
        summary_progress = SummaryProgressCallback()
        run_control = RunControl()
        interrupt_tracker = UserInputInterruptTracker()
        with self._run_lock:
            if self._state is _ExecutorState.CLOSED:
                raise RuntimeError("智能体运行时资源已经关闭")
            if self._state is not _ExecutorState.READY:
                raise RuntimeError("AgentExecutor 只允许执行一次")
            self._state = _ExecutorState.RUNNING
            self._run_control = run_control
            self._cancel_requested = cancel_requested
        effective_config = merge_configs(
            self._resources.runtime_context.conversation.to_config(),
            {
                "run_id": self._observer.run_id,
                "callbacks": [self._observer, interrupt_tracker, summary_progress],
            },
        )
        try:
            # 3.过滤伪装成工具错误的用户输入中断，保留其余原始事件。
            stream = self._graph.stream_events(
                Command(resume=dict(resume)) if resume is not None else {"messages": [message]},
                config=effective_config,
                context=self._resources.runtime_context,
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
            # 4.登记等待输入的中断身份，供结果状态判定等待输入。
            if stream.interrupted:
                self._interrupts = tuple(
                    {"interrupt_id": item.id, "namespace": [], "payload": item.value}
                    for item in stream.interrupts
                )
        except BaseException:
            # 5.任何退出路径都先结束执行状态，资源关闭统一在 execute 清理。
            self._finish_run()
            raise
        else:
            self._finish_run()
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
            # 1.持续记录根状态快照，供结果构建和审核使用。
            if raw_event.get("method") == "values" and not params.get("namespace") and isinstance(data, Mapping):
                state.clear()
                state.update(data)
            # 2.取消请求后先请求排空；排空失败说明图无法协作，立即中断消费。
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
        """统一完成状态、回答与用量。"""
        messages = state.get("messages")
        answer = final_answer(messages) if isinstance(messages, list) else ""
        # 1.状态优先级：错误 > 取消 > 等待输入 > 完成。
        status: Literal["completed", "cancelled", "failed", "waiting_input"] = "completed"
        if error is not None:
            status = "failed"
        elif cancelled:
            status = "cancelled"
        elif self._interrupts:
            status = "waiting_input"
        self._observer.finish()
        return AgentExecutionResult(
            status=status,
            interrupts=self._interrupts if status == "waiting_input" else (),
            answer=answer,
            state=json_value(state),
            usage=self._observer.build_run_usage(),
            error=error,
        )

    def _finish_run(self) -> None:
        """结束执行状态并释放控制柄；资源统一由 execute 的清理路径关闭。"""
        with self._run_lock:
            self._state = _ExecutorState.FINISHED
            self._run_control = None
            self._cancel_requested = None


def _close_safely(stage: str, close: Callable[[], None], error: Exception | None) -> Exception | None:
    """执行一个清理步骤，返回新错误；已有错误时只记日志不覆盖。"""
    try:
        close()
    except Exception as exc:
        logger.exception("%s失败", stage)
        return exc if error is None else error
    return error


__all__ = ["AgentExecutor"]
