"""Agent V2 单次 Deep Agents 图执行与资源生命周期。

``execute`` 管理执行与资源收尾，``_run_graph`` 消费框架流；取消监控线程请求图排空。
"""

from collections.abc import Callable, Mapping
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
from internal.core.agent_v2.execution.event_normalizer import AgentEventNormalizer
from internal.core.agent_v2.execution.json_serialization import to_json_value
from internal.core.agent_v2.execution.message_text import extract_final_answer
from internal.core.agent_v2.execution.runtime_resources import AgentRuntimeResources
from internal.core.agent_v2.execution.model_progress import ModelProgressCallback
from internal.core.agent_v2.execution.user_input_interrupt_tracker import UserInputInterruptTracker
from internal.core.agent_v2.observability.run_observer import PlatformAgentRunObserver
from internal.extension.logging_extension import logger


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
        self._state: Literal["ready", "running", "finished", "closed"] = "ready"
        self._run_control: RunControl | None = None
        self._cancel_requested: Event | None = None

    def execute(
        self,
        message: BaseMessage | None = None,
        *,
        resume: Mapping[str, Any] | None = None,
        on_event: Callable[[AgentEvent], None] | None = None,
        cancel_requested: Event | None = None,
    ) -> AgentExecutionResult:
        """消息与恢复答案二选一；统一交付结果并关闭资源，事件订阅和取消信号可选。"""
        # 1.登记本次执行；重复调用在清理范围外拒绝，避免释放正在使用的资源。
        if cancel_requested is None:
            cancel_requested = Event()
        state: dict[str, Any] = {}
        interrupts: tuple[dict[str, Any], ...] = ()
        error: Exception | None = None
        run_control = RunControl()
        with self._run_lock:
            if self._state == "closed":
                raise RuntimeError("智能体运行时资源已经关闭")
            if self._state != "ready":
                raise RuntimeError("AgentExecutor 只允许执行一次")
            self._state = "running"
            self._run_control = run_control
            self._cancel_requested = cancel_requested
        try:
            # 2.输入错误直接上抛，但仍释放本次资源；已取消时不启动图。
            if (message is None) == (resume is None):
                raise ValueError("必须且只能提供输入消息或恢复答案")
            if message is not None and not isinstance(message, BaseMessage):
                raise ValueError("输入消息必须是 BaseMessage")
            if resume is not None and not isinstance(resume, Mapping):
                raise ValueError("恢复答案必须是中断标识到答案的映射")
            # 3.图执行负责自己的事件流和回调，异常时也返回已收集的根状态。
            state, interrupts, error = self._run_graph(
                message=message, resume=resume, on_event=on_event,
                cancel_requested=cancel_requested, control=run_control,
            )
        finally:
            # 4.框架流退出后才释放控制柄与资源；配置构造失败也走同一清理路径。
            with self._run_lock:
                self._state = "finished"
                self._run_control = None
                self._cancel_requested = None
            try:
                self.close()
            except Exception as exc:
                logger.exception("Agent 运行资源关闭失败")
                if error is None:
                    error = exc
        # 5.错误优先于取消和等待输入；结果只使用本次执行的局部状态。
        status: Literal["completed", "cancelled", "failed", "waiting_input"] = "completed"
        if error is not None:
            status = "failed"
        elif cancel_requested.is_set():
            status = "cancelled"
        elif interrupts:
            status = "waiting_input"
        messages = state.get("messages")
        self._observer.finish()
        return AgentExecutionResult(
            status=status,
            interrupts=interrupts if status == "waiting_input" else (),
            answer=extract_final_answer(messages) if isinstance(messages, list) else "",
            state=to_json_value(state),
            usage=self._observer.build_run_usage(),
            error=error,
        )

    def close(self) -> None:
        """串行清理非运行态资源；失败是终态，不能重跑已释放会话的同步流程。"""
        with self._run_lock:
            if self._state == "running":
                raise RuntimeError("智能体仍在运行，不能提前关闭运行时资源")
            if self._state == "closed":
                return
            self._resources.close()
            self._state = "closed"

    def request_cancel(self) -> bool:
        """停止新增内容并请求图排空；未完成消息由业务终态收敛，未运行时返回 False。"""
        with self._run_lock:
            control = self._run_control
            if (self._state != "running"
                    or control is None or self._cancel_requested is None):
                return False
            self._cancel_requested.set()
        control.request_drain("cancel_requested")
        return True

    def _run_graph(
        self,
        *,
        message: BaseMessage | None,
        resume: Mapping[str, Any] | None,
        on_event: Callable[[AgentEvent], None] | None,
        cancel_requested: Event,
        control: RunControl,
    ) -> tuple[dict[str, Any], tuple[dict[str, Any], ...], Exception | None]:
        """完成一次图执行，返回根状态、中断和错误；取消后排空但停止交付新事件。"""
        if cancel_requested.is_set():
            return {}, (), None

        state: dict[str, Any] = {}
        interrupts: tuple[dict[str, Any], ...] = ()
        error: Exception | None = None
        normalizer = AgentEventNormalizer()
        model_progress = ModelProgressCallback()
        interrupt_tracker = UserInputInterruptTracker()
        try:
            # 1.每次执行独立绑定统计、中断和进度回调，避免跨片段保留模型调用状态。
            config = merge_configs(
                self._resources.runtime_context.conversation.to_config(),
                {
                    "run_id": self._observer.run_id,
                    "callbacks": [self._observer, interrupt_tracker, model_progress],
                },
            )
            # 2.新消息走普通输入：LangGraph 丢弃旧调度，DeepAgents 补齐被取消的工具回执。
            # 只有已接受的表单提交才走 resume；不能将停止后的下一轮当成旧中断恢复。
            with self._graph.stream_events(
                Command(resume=dict(resume)) if resume is not None else {"messages": [message]},
                config=config, context=self._resources.runtime_context,
                version="v3", control=control,
            ) as stream:
                for raw_event in stream:
                    params = raw_event["params"]
                    data = params["data"]
                    channel = raw_event["method"]
                    # 3.排空期间仍保存根状态，子图快照不能覆盖最终回答。
                    if channel == "values" and not params["namespace"] and isinstance(data, Mapping):
                        state = dict(data)
                    if cancel_requested.is_set():
                        if not control.drain_requested:
                            self.request_cancel()
                        continue
                    # 4.ask_user 的 GraphInterrupt 是正常暂停，其余公开事件按序交付。
                    if (channel == "tools" and isinstance(data, Mapping)
                            and data.get("event") == "tool-error"
                            and interrupt_tracker.is_interrupted(data.get("tool_call_id"))):
                        continue
                    events = normalizer.accept(raw_event)
                    if on_event is None:
                        continue
                    for event in events:
                        on_event(event)
                # 5.流退出后收集有效中断，业务层据此保存当前待填批次。
                if stream.interrupted:
                    interrupts = tuple(
                        {"interrupt_id": item.id, "namespace": [], "payload": item.value}
                        for item in stream.interrupts
                    )
        except GraphDrained as exc:
            if exc.reason == "cancel_requested":
                cancel_requested.set()
            elif not cancel_requested.is_set():
                error = exc
        except Exception as exc:
            error = exc
        finally:
            # 6.无论完成、暂停还是失败，都释放回调持有的子图 writer。
            try:
                model_progress.close()
            except Exception as exc:
                logger.exception("Agent 模型进度关闭失败")
                if error is None:
                    error = exc
        return state, interrupts, error


__all__ = ["AgentExecutor"]
