"""进程内 Run 跟踪与事件落库：注册、取消信号、订阅通知和批量持久化。"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext
from uuid import UUID

from internal.core.agent_v2 import (
    AgentEvent,
)
from internal.entity import AgentExecutionSegment
from internal.extension.logging_extension import logger
from internal.schema.agent.agent_protocol_schema import AgentProtocolEvent
from internal.service.agent_protocol.event_projector import (
    AgentProtocolEventProjector, is_tool_argument_delta, normalize_custom_data,
)
from internal.service.agent_protocol.state_projector import adapt_graph_state
from .live_event_stream import LiveEventStream

_FRAMEWORK_TERMINAL_EVENTS = frozenset({"completed", "failed", "interrupted", "drained", "cancelled"})


class EventSignal:
    """进程级通知版本；先取版本再读取内存或数据库，避免漏掉并发发布。"""

    def __init__(self) -> None:
        """只维护一个通知版本，不按会话分桶也不保存事件正文。"""
        self._condition = threading.Condition()
        self._version = 0

    def snapshot(self) -> int:
        """在读取事件前取得版本，并发发布也会触发下一次读取。"""
        with self._condition:
            return self._version

    def notify(self) -> None:
        """实时发布、提交或执行退出后唤醒订阅者；订阅者自行筛选 Run。"""
        with self._condition:
            self._version += 1
            self._condition.notify_all()

    def wait(self, version: int, *, timeout: float) -> None:
        """进程内通知必达，超时只作为调用方自定义的等待上限。"""
        with self._condition:
            self._condition.wait_for(lambda: self._version != version, timeout=timeout)


event_signal = EventSignal()


class AgentRunTracker:
    """管理本进程的活动 Run；只做内存登记与取消信号，不读写数据库。"""

    def __init__(self) -> None:
        """锁保护执行片段与实时尾部的关联，持久化由独立写线程负责。"""
        self._runs: dict[str, AgentExecutionSegment] = {}
        self._streams: dict[str, LiveEventStream] = {}
        self._lock = threading.RLock()

    def get(self, conversation_id: UUID) -> AgentExecutionSegment | None:
        """读取会话的活动 Run，应用与入口归属由调用方自行校验。"""
        with self._lock:
            return self._runs.get(str(conversation_id))

    def register(self, run: AgentExecutionSegment) -> None:
        """登记当前会话的新 Run；覆盖式写入依赖会话内至多一个活动 Run。"""
        with self._lock:
            previous = self._runs.get(str(run.conversation_id))
            if previous is not None:
                self._streams.pop(str(previous.run_id), None)
            self._runs[str(run.conversation_id)] = run
            self._streams[str(run.run_id)] = LiveEventStream(run.initial_event_sequence)

    def stream(self, conversation_id: UUID, run_id: UUID) -> LiveEventStream | None:
        """只返回当前已登记片段的缓冲；HTTP 归属校验仍由查询入口完成。"""
        with self._lock:
            run = self._runs.get(str(conversation_id))
            if run is None or run.run_id != run_id:
                return None
            return self._streams.get(str(run_id))

    def publish(self, run: AgentExecutionSegment, events: list[AgentProtocolEvent]) -> None:
        """拒绝旧片段向恢复后的执行串入事件，先发布再唤醒等待者。"""
        with self._lock:
            if self._runs.get(str(run.conversation_id)) is not run:
                return
            self._streams[str(run.run_id)].publish(events)
        event_signal.notify()

    def finish(self, run: AgentExecutionSegment) -> None:
        """结束本地 Run，旧 Run 不得移除后继 Run。"""
        with self._lock:
            run.done.set()
            conversation_key = str(run.conversation_id)
            if self._runs.get(conversation_key) is run:
                self._runs.pop(conversation_key, None)
                self._streams.pop(str(run.run_id), None)
        event_signal.notify()

    def active_run_ids(self) -> set[UUID]:
        """返回本进程仍在执行的 Run 标识，供失联收敛区分存活与孤儿记录。"""
        with self._lock:
            return {
                run.run_id for run in self._runs.values() if not run.done.is_set()
            }

    def request_cancel(self, conversation_id: UUID, run_id: UUID) -> bool:
        """只置本进程内目标 Run 的取消信号；归属校验和取消状态由调用方负责。"""
        with self._lock:
            run = self._runs.get(str(conversation_id))
            if run is None or run.run_id != run_id:
                return False
            run.cancel_requested.set()
        return True


class AgentRunEventBuffer:
    """事件先实时发布，独立线程有界合批保存；结束时等待持久化收尾。"""

    _MAX_PENDING = 512
    _FLUSH_INTERVAL = 0.25

    def __init__(
        self,
        run: AgentExecutionSegment,
        projector: AgentProtocolEventProjector,
        commit_events: Callable[[AgentExecutionSegment, list[AgentProtocolEvent]], None],
        *,
        started_at: float,
        batch_size: int,
        publish_event: Callable[[AgentProtocolEvent], None],
        worker_context: Callable[[], AbstractContextManager] = nullcontext,
    ) -> None:
        """写线程由执行片段独占，数据库上下文由 Service 注入，禁止跨线程共用 Session。"""
        self._run = run
        self._projector = projector
        self._commit_events = commit_events
        self._started_at = started_at
        self._batch_size = batch_size
        self._publish_event = publish_event
        self._pending: deque[AgentProtocolEvent] = deque()
        self._condition = threading.Condition()
        self._closing = False
        self._error: Exception | None = None
        self._first_text_published = False
        self._worker = threading.Thread(
            target=self._write_batches, args=(worker_context,), name="agent-event-writer", daemon=True,
        )
        self._worker.start()

    def receive(self, agent_event: AgentEvent) -> None:
        """按顺序公开展示事件，写库不阻塞正常的事件接收和交付。"""
        # 1.根终态由业务事务发布，不能提前用框架完成事件结束前端订阅。
        data = agent_event.data
        channel = agent_event.channel
        is_root = not agent_event.namespace
        event_name = data.get("event")
        if channel == "lifecycle" and is_root and (event_name or data.get("status")) in _FRAMEWORK_TERMINAL_EVENTS:
            return

        # 2.在接收点适配公开载荷，助手消息直接绑定当前 Run 的业务消息 ID。
        if channel == "custom":
            data = normalize_custom_data(data)
            if data is None:
                return
        elif channel == "values":
            data = adapt_graph_state(data)
        elif channel == "messages":
            if is_tool_argument_delta(data):
                return
            is_assistant_start = event_name == "message-start" and data.get("role") in {"ai", "assistant"}
            if is_root and is_assistant_start:
                data = {**data, "id": f"{self._run.spec.turn_id}:assistant"}

        # 3.使用与业务事件共用的编号器，登记待写队列并立即发布。
        event = self._projector.create_event(
            channel, data, namespace=agent_event.namespace, timestamp=agent_event.timestamp,
            node=agent_event.node, source="agent_v2", source_sequence=agent_event.sequence,
            model_run_id=agent_event.model_run_id,
        )
        self.publish(event)
        event_name = data.get("event")

        # 4.首字计时只认文本增量，推理、工具和空文本不计入。
        delta = data.get("delta")
        first_text = (
            not self._first_text_published
            and event.method == "messages" and event_name == "content-block-delta"
            and isinstance(delta, Mapping) and delta.get("type") == "text-delta"
            and bool(delta.get("text"))
        )

        # 5.首字计时对应内存发布，不再把数据库耗时算成模型首字交付。
        if not first_text:
            return
        self._first_text_published = True
        duration_ms = (time.monotonic() - self._started_at) * 1000
        logger.info(
            "agent_delivery_timing run_id=%s stage=first_text duration_ms=%.2f",
            self._run.run_id, duration_ms,
        )

    def finish(self) -> None:
        """停止接收并等待尾批；失败必须上报，调用方不能发布成功终态。"""
        # 1.先回收写线程，确保下面的补写不会与后台提交并发。
        self.close()
        if self._error is not None:
            # 2.按原顺序补写失败批及尾部；再次失败直接交给业务层结算为失败。
            while self._pending:
                batch = list(self._pending)[:self._batch_size]
                self._commit_events(self._run, batch)
                self._run.last_event_sequence = batch[-1].seq
                for _ in batch:
                    self._pending.popleft()
            # 3.全部补写成功后才清除错误并通知订阅者。
            self._error = None
            event_signal.notify()

    def close(self) -> None:
        """所有退出路径都回收写线程；旧线程不能在本轮结算后继续写库。"""
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._worker.join()

    def publish(self, event: AgentProtocolEvent) -> None:
        """先登记待保存事件再实时交付；仅在写入持续落后时用背压限制内存。"""
        with self._condition:
            # 1.待写队列包含正在提交的批次，背压不能只计算尚未取出的事件。
            self._condition.wait_for(
                lambda: len(self._pending) < self._MAX_PENDING or self._error or self._closing,
            )
            if self._error is not None:
                raise self._error
            if self._closing:
                raise RuntimeError("Agent event writer is closed")
            # 2.登记和实时发布在同一锁内完成，写线程不能抢先提交未发布的事件。
            self._pending.append(event)
            self._publish_event(event)
            self._condition.notify_all()

    def _write_batches(self, worker_context: Callable[[], AbstractContextManager]) -> None:
        """空闲时也按截止时间提交小批次；提交失败保留队列并唤醒生产者。"""
        try:
            with worker_context():
                while True:
                    with self._condition:
                        # 1.等待首条事件，再按数量或时间组成批次；关闭时立即处理尾批。
                        self._condition.wait_for(lambda: self._pending or self._closing)
                        if not self._pending:
                            return
                        self._condition.wait_for(
                            lambda: len(self._pending) >= self._batch_size or self._closing,
                            timeout=self._FLUSH_INTERVAL,
                        )
                        batch = list(self._pending)[:self._batch_size]
                    # 2.数据库提交在锁外执行，期间生产者仍可实时发布后续事件。
                    self._commit_events(self._run, batch)
                    with self._condition:
                        # 3.只有提交成功才移除队列前缀并推进已保存水位。
                        self._run.last_event_sequence = batch[-1].seq
                        for _ in batch:
                            self._pending.popleft()
                        self._condition.notify_all()
                    event_signal.notify()
        except Exception as exc:
            # 4.保留失败批，唤醒生产者停止接收；finish() 在本线程退出后负责补写。
            with self._condition:
                self._error = exc
                self._condition.notify_all()


__all__ = ["AgentRunEventBuffer", "AgentRunTracker", "EventSignal", "event_signal"]
