"""进程内 Run 跟踪与事件落库：注册、取消信号、订阅通知和批量持久化。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from uuid import UUID

from internal.core.agent_v2 import (
    AgentEvent,
)
from internal.entity import AgentRunExecution
from internal.extension.logging_extension import logger
from internal.schema.agent.agent_protocol_schema import AgentProtocolEvent
from internal.service.agent_protocol.event_projector import AgentProtocolEventProjector


class EventSignal:
    """进程级事件通知版本；订阅端先取版本再查库，事件正文一律来自数据库。"""

    def __init__(self) -> None:
        """只维护一个通知版本，不按会话分桶也不保存事件正文。"""
        self._condition = threading.Condition()
        self._version = 0

    def snapshot(self) -> int:
        """在读取数据库前取得版本，查询期间的提交也会触发下一次读取。"""
        with self._condition:
            return self._version

    def notify(self) -> None:
        """提交后唤醒本进程所有订阅者；唤醒结果由订阅端按会话自行过滤。"""
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
        """条件锁只保护本地注册表，事件正文由数据库保存。"""
        self._runs: dict[str, AgentRunExecution] = {}
        self._lock = threading.RLock()

    def get(self, conversation_id: UUID) -> AgentRunExecution | None:
        """读取会话的活动 Run，应用与入口归属由调用方自行校验。"""
        with self._lock:
            return self._runs.get(str(conversation_id))

    def register(self, run: AgentRunExecution) -> None:
        """登记当前会话的新 Run；覆盖式写入依赖会话内至多一个活动 Run。"""
        with self._lock:
            self._runs[str(run.conversation_id)] = run

    def finish(self, run: AgentRunExecution) -> None:
        """结束本地 Run，旧 Run 不得移除后继 Run。"""
        with self._lock:
            run.done.set()
            conversation_key = str(run.conversation_id)
            if self._runs.get(conversation_key) is run:
                self._runs.pop(conversation_key, None)

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
    """集中管理流式批次及首字计时，所有通知都发生在数据库提交之后。"""

    def __init__(
        self,
        run: AgentRunExecution,
        projector: AgentProtocolEventProjector,
        commit_events: Callable[[AgentRunExecution, list[AgentProtocolEvent]], None],
        *,
        started_at: float,
        batch_size: int,
    ) -> None:
        """缓冲只由本次执行的事件回调使用，不拥有 Agent、数据库 Session 或持久化实现。"""
        self._run = run
        self._projector = projector
        self._commit_events = commit_events
        self._started_at = started_at
        self._batch_size = batch_size
        self._pending: list[AgentProtocolEvent] = []
        self._last_flush_at = time.monotonic()
        self._first_text_published = False

    def receive(self, agent_event: AgentEvent) -> None:
        """按事件到达顺序缓冲，首字和阶段边界立即提交。"""
        # 1.Core 已完成框架解析；这里仅绑定业务序号和公开通道。
        event = self._projector.project(agent_event)
        if event is None:
            return
        # 2.投影器只返回已校验、可重放的公开事件。
        self._pending.append(event)
        data = event.params.data
        event_name = data.get("event") if isinstance(data, Mapping) else None

        # 3.首字计时只认文本增量，推理、工具和空文本不计入。
        delta = data.get("delta") if isinstance(data, Mapping) else None
        is_text_delta = (
            event.method == "messages" and event_name == "content-block-delta"
            and isinstance(delta, Mapping) and delta.get("type") == "text-delta"
        )
        first_text = is_text_delta and not self._first_text_published and bool(delta.get("text"))

        # 4.逐项判断刷新原因；未命中时由数量或时间阈值兜底。
        if first_text:
            flush_now = True  # 首字立即交付，不能被批量阈值拖慢。
        elif event.method != "messages":
            flush_now = True  # 工具、状态等事件立即交付，先刷出它们之前的文本。
        elif event_name in {"content-block-finish", "message-finish"}:
            flush_now = True  # 消息阶段已结束，不能把尾批留到下一阶段。
        elif len(self._pending) >= self._batch_size:
            flush_now = True  # 已达到单批数量上限。
        elif time.monotonic() - self._last_flush_at >= 0.03:
            flush_now = True  # 有新事件到达时检查时间阈值，不另建定时线程；窗口取 ~30ms 平衡打字机流畅度与提交频率。
        if not flush_now:
            return

        # 5.先落库再计时；只有首字成功交付后才记录首字耗时。
        self._flush()
        if not first_text:
            return
        self._first_text_published = True
        duration_ms = (self._last_flush_at - self._started_at) * 1000
        logger.info(
            "agent_delivery_timing run_id=%s stage=first_text duration_ms=%.2f",
            self._run.run_id, duration_ms,
        )

    def finish(self) -> None:
        """刷新 Core 已交付的尾批；失败时保留批次供上层处理原始错误。"""
        self._flush()

    def publish(self, event: AgentProtocolEvent) -> None:
        """直接发布已投影的单个事件；落库、游标与通知走与批量刷新同一条路。"""
        self._pending.append(event)
        self._flush()

    def _flush(self) -> None:
        """提交失败不推进游标；提交成功后才推进游标并唤醒订阅者。"""
        if not self._pending:
            return
        # 先提交数据库，成功后推进游标并唤醒订阅者。
        self._commit_events(self._run, self._pending)
        self._run.last_event_sequence = self._pending[-1].seq
        event_signal.notify()
        self._pending.clear()
        self._last_flush_at = time.monotonic()


__all__ = ["AgentRunEventBuffer", "AgentRunTracker", "EventSignal", "event_signal"]
