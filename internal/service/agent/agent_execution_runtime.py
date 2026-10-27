"""进程内执行跟踪与事件落库：注册、取消信号、订阅通知和批量持久化。"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from uuid import UUID

from internal.entity import AgentExecutionHandle
from internal.extension.logging_extension import logger
from internal.schema.agent.agent_protocol_schema import AgentProtocolEvent
from .agent_execution_event_hub import AgentExecutionEventHub


class AgentExecutionTracker:
    """管理活动执行及其事件中心；数据库读写仍由 Service 和 Query 负责。"""

    def __init__(self) -> None:
        """锁保护执行片段与实时尾部的关联，持久化由独立写线程负责。"""
        self._runs_by_conversation: dict[str, AgentExecutionHandle] = {}
        self._hubs_by_run: dict[str, AgentExecutionEventHub] = {}
        self._lock = threading.RLock()

    def register(self, run: AgentExecutionHandle) -> None:
        """登记当前会话的新执行；覆盖式写入依赖会话内至多一个活动执行。"""
        with self._lock:
            previous = self._runs_by_conversation.get(str(run.conversation_id))
            if previous is not None:
                previous_hub = self._hubs_by_run.pop(str(previous.run_id), None)
                if previous_hub is not None:
                    previous_hub.close()
            self._runs_by_conversation[str(run.conversation_id)] = run
            self._hubs_by_run[str(run.run_id)] = AgentExecutionEventHub(
                run.initial_event_sequence
            )

    def get(self, conversation_id: UUID) -> AgentExecutionHandle | None:
        """读取会话的活动执行，应用与入口归属由调用方自行校验。"""
        with self._lock:
            return self._runs_by_conversation.get(str(conversation_id))

    def event_hub(self, conversation_id: UUID, run_id: UUID) -> AgentExecutionEventHub | None:
        """返回当前执行的事件中心；HTTP 归属校验仍由查询入口完成。"""
        with self._lock:
            run = self._runs_by_conversation.get(str(conversation_id))
            if run is None or run.run_id != run_id:
                return None
            return self._hubs_by_run.get(str(run_id))

    def publish_events(
        self, run: AgentExecutionHandle, events: list[AgentProtocolEvent]
    ) -> None:
        """拒绝旧片段向恢复后的执行串入事件，先发布再唤醒等待者。"""
        with self._lock:
            if self._runs_by_conversation.get(str(run.conversation_id)) is not run:
                return
            self._hubs_by_run[str(run.run_id)].publish(events)

    def finish(self, run: AgentExecutionHandle) -> None:
        """结束本地执行，旧执行不得移除后继执行。"""
        with self._lock:
            run.done.set()
            conversation_key = str(run.conversation_id)
            if self._runs_by_conversation.get(conversation_key) is run:
                self._runs_by_conversation.pop(conversation_key, None)
                hub = self._hubs_by_run.pop(str(run.run_id), None)
                if hub is not None:
                    hub.close()

    def active_run_ids(self) -> set[UUID]:
        """返回本进程仍在执行的标识，供失联收敛区分存活与孤儿记录。"""
        with self._lock:
            return {
                run.run_id
                for run in self._runs_by_conversation.values()
                if not run.done.is_set()
            }

    def request_cancel(self, conversation_id: UUID, run_id: UUID) -> bool:
        """只置本进程内目标 Run 的取消信号；归属校验和取消状态由调用方负责。"""
        with self._lock:
            run = self._runs_by_conversation.get(str(conversation_id))
            if run is None or run.run_id != run_id:
                return False
            run.cancel_requested.set()
        return True


class AgentExecutionEventBuffer:
    """事件先实时发布，独立线程有界合批保存；结束时等待持久化收尾。"""

    _MAX_PENDING = 512
    _FLUSH_INTERVAL = 0.25

    def __init__(
        self,
        run: AgentExecutionHandle,
        commit_events: Callable[
            [AgentExecutionHandle, list[AgentProtocolEvent]], None
        ],
        *,
        started_at: float,
        batch_size: int,
        publish_realtime: Callable[[AgentProtocolEvent], None],
        worker_context: Callable[[], AbstractContextManager] = nullcontext,
    ) -> None:
        """写线程由执行片段独占，数据库上下文由 Service 注入，禁止跨线程共用 Session。"""
        self._run = run
        self._commit_events = commit_events
        self._started_at = started_at
        self._batch_size = batch_size
        self._publish_realtime = publish_realtime
        self._pending_events: deque[AgentProtocolEvent] = deque()
        self._condition = threading.Condition()
        self._closing = False
        self._error: Exception | None = None
        self._first_text_published = False
        self._worker = threading.Thread(
            target=self._run_writer,
            args=(worker_context,),
            name="agent-event-writer",
            daemon=True,
        )
        self._worker.start()

    def drain_and_close(self) -> None:
        """停止接收并等待尾批；失败必须上报，调用方不能发布成功终态。"""
        # 1.先回收写线程，确保下面的补写不会与后台提交并发。
        self.close()
        if self._error is not None:
            # 2.按原顺序补写失败批及尾部；再次失败直接交给业务层结算为失败。
            while self._pending_events:
                batch = list(self._pending_events)[: self._batch_size]
                self._commit_events(self._run, batch)
                self._run.last_event_sequence = batch[-1].seq
                for _ in batch:
                    self._pending_events.popleft()
            # 3.全部补写成功后才清除错误并通知订阅者。
            self._error = None

    def close(self) -> None:
        """所有退出路径都回收写线程；旧线程不能在本轮结算后继续写库。"""
        with self._condition:
            self._closing = True
            self._condition.notify_all()
        self._worker.join()

    def emit(self, event: AgentProtocolEvent, *, is_text_delta: bool = False) -> None:
        """登记一条统一事件并立即实时交付；写入落后时通过背压限制内存。"""
        with self._condition:
            # 1.待写队列包含正在提交的批次，背压不能只计算尚未取出的事件。
            self._condition.wait_for(
                lambda: len(self._pending_events) < self._MAX_PENDING
                or self._error
                or self._closing,
            )
            if self._error is not None:
                raise self._error
            if self._closing:
                raise RuntimeError("Agent event writer is closed")
            # 2.登记和实时发布在同一锁内完成，写线程不能抢先提交未发布的事件。
            self._pending_events.append(event)
            self._publish_realtime(event)
            self._condition.notify_all()
        # 发布成功后才计首字；格式识别由消息解析器完成。
        if is_text_delta and not self._first_text_published:
            self._first_text_published = True
            logger.info(
                "agent_delivery_timing run_id=%s stage=first_text duration_ms=%.2f",
                self._run.run_id, (time.monotonic() - self._started_at) * 1000,
            )

    def _run_writer(self, worker_context: Callable[[], AbstractContextManager]) -> None:
        """空闲时也按截止时间提交小批次；提交失败保留队列并唤醒生产者。"""
        try:
            with worker_context():
                while True:
                    with self._condition:
                        # 1.等待首条事件，再按数量或时间组成批次；关闭时立即处理尾批。
                        self._condition.wait_for(
                            lambda: self._pending_events or self._closing
                        )
                        if not self._pending_events:
                            return
                        self._condition.wait_for(
                            lambda: len(self._pending_events) >= self._batch_size
                            or self._closing,
                            timeout=self._FLUSH_INTERVAL,
                        )
                        batch = list(self._pending_events)[: self._batch_size]
                    # 2.数据库提交在锁外执行，期间生产者仍可实时发布后续事件。
                    self._commit_events(self._run, batch)
                    with self._condition:
                        # 3.只有提交成功才移除队列前缀并推进已保存水位。
                        self._run.last_event_sequence = batch[-1].seq
                        for _ in batch:
                            self._pending_events.popleft()
                        self._condition.notify_all()
        except Exception as exc:
            # 4.保留失败批，唤醒生产者停止接收；drain_and_close() 在本线程退出后负责补写。
            with self._condition:
                self._error = exc
                self._condition.notify_all()


__all__ = ["AgentExecutionEventBuffer", "AgentExecutionTracker"]
