"""进程内 Run 跟踪与事件落库：注册、取消信号、订阅通知和批量持久化。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import UUID

from internal.core.agent_v2 import (
    AgentEvent,
)
from internal.entity import AgentRunExecution
from internal.entity.conversation_entity import InvokeFrom
from internal.extension.logging_extension import logger
from internal.schema.agent.agent_workbench_schema import WorkbenchEvent
from internal.service.agent_protocol.event_projector import AgentProtocolEventProjector


@dataclass
class _EventSignal:
    """只维护会话通知版本；先查版本再查库可避免丢失唤醒。"""

    condition: threading.Condition = field(default_factory=threading.Condition)
    version: int = 0
    subscribers: int = 0

    def snapshot(self) -> int:
        """在读取数据库前取得版本，查询期间的提交也会触发下一次读取。"""
        with self.condition:
            return self.version

    def notify(self) -> None:
        """仅唤醒同一会话和入口的订阅者。"""
        with self.condition:
            self.version += 1
            self.condition.notify_all()

    def wait(self, version: int, *, timeout: float) -> None:
        """通知已经到达时立即返回，超时兜底覆盖其他进程的提交。"""
        with self.condition:
            self.condition.wait_for(lambda: self.version != version, timeout=timeout)


class AgentRunTracker:
    """管理本进程的活动 Run；只做内存登记与通知，不读写数据库。"""

    def __init__(self) -> None:
        """条件锁只保护本地注册表，事件正文由数据库保存。"""
        self._runs: dict[str, AgentRunExecution] = {}
        self._lock = threading.RLock()
        self._signals: dict[tuple[str, InvokeFrom], _EventSignal] = {}

    def get(
        self,
        conversation_id: UUID,
        *,
        app_id: UUID | None = None,
        invoke_from: InvokeFrom | None = None,
    ) -> AgentRunExecution | None:
        """读取会话的活动 Run，入口不匹配时视为不存在。"""
        with self._lock:
            run = self._runs.get(str(conversation_id))
            if run is None:
                return None
            if app_id is not None and run.spec.app_id != app_id:
                return None
            if invoke_from is not None and run.spec.invoke_from != invoke_from:
                return None
            return run

    def register(self, run: AgentRunExecution) -> None:
        """登记当前会话的新 Run，并通知等待该会话的订阅者。"""
        with self._lock:
            self._runs[str(run.conversation_id)] = run
            self.notify_events(run.conversation_id, run.spec.invoke_from)

    def finish(self, run: AgentRunExecution) -> None:
        """结束本地 Run 并唤醒订阅者，旧 Run 不得移除后继 Run。"""
        with self._lock:
            run.done.set()
            conversation_key = str(run.conversation_id)
            if self._runs.get(conversation_key) is run:
                self._runs.pop(conversation_key)
            self.notify_events(run.conversation_id, run.spec.invoke_from)

    def active_run_ids(self) -> set[UUID]:
        """返回本进程仍在执行的 Run 标识，供失联收敛区分存活与孤儿记录。"""
        with self._lock:
            return {
                run.run_id for run in self._runs.values() if not run.done.is_set()
            }

    def notify_events(self, conversation_id: UUID, invoke_from: InvokeFrom) -> None:
        """已提交事件后唤醒本进程订阅者，不重复保存事件正文。"""
        with self._lock:
            signal = self._signals.get((str(conversation_id), invoke_from))
            if signal is not None:
                signal.notify()

    def request_cancel(
        self,
        conversation_id: UUID,
        run_id: UUID,
        *,
        app_id: UUID | None = None,
        invoke_from: InvokeFrom | None = None,
    ) -> bool:
        """只通知本进程内的目标 Run，数据库取消状态由调用方负责。"""
        with self._lock:
            run = self.get(
                conversation_id,
                app_id=app_id,
                invoke_from=invoke_from,
            )
            if run is None or run.run_id != run_id:
                return False
            if not run.done.is_set():
                run.cancel_requested.set()
                self.notify_events(run.conversation_id, run.spec.invoke_from)
            return True

    @contextmanager
    def watch_events(self, conversation_id: UUID, invoke_from: InvokeFrom) -> Iterator[_EventSignal]:
        """订阅建立前登记通知，最后一个连接断开时清理会话信号。"""
        key = (str(conversation_id), invoke_from)
        with self._lock:
            signal = self._signals.setdefault(key, _EventSignal())
            signal.subscribers += 1
        try:
            yield signal
        finally:
            with self._lock:
                signal.subscribers -= 1
                if signal.subscribers == 0:
                    self._signals.pop(key, None)


class AgentRunEventBuffer:
    """集中管理流式批次及首字计时，所有通知都发生在数据库提交之后。"""

    def __init__(
        self,
        run: AgentRunExecution,
        projector: AgentProtocolEventProjector,
        tracker: AgentRunTracker,
        commit_events: Callable[[AgentRunExecution, list[WorkbenchEvent]], None],
        *,
        started_at: float,
        batch_size: int,
    ) -> None:
        """缓冲只由本次执行的事件回调使用，不拥有 Agent、数据库 Session 或持久化实现。"""
        self._run = run
        self._projector = projector
        self._tracker = tracker
        self._commit_events = commit_events
        self._started_at = started_at
        self._batch_size = batch_size
        self._pending: list[WorkbenchEvent] = []
        self._last_flush_at = time.monotonic()
        self._first_text_published = False
        self._first_reasoning_published = False

    def receive(self, agent_event: AgentEvent) -> None:
        """按事件到达顺序缓冲，首段推理、首字和阶段边界立即提交。"""
        # 1.Core 已完成框架解析；这里仅绑定业务序号和公开通道。
        event = self._projector.project(agent_event)
        if event is None:
            return
        # 1.投影器只返回已校验、可重放的公开事件。
        self._pending.append(event)
        data = event.params.data
        event_name = data.get("event") if isinstance(data, Mapping) else None

        # 2.首字计时只认文本增量，推理、工具和空文本不计入。
        first_text = False
        delta = data.get("delta") if isinstance(data, Mapping) else None
        is_text_message = event.method == "messages" and event_name == "content-block-delta"
        is_text_delta = isinstance(delta, Mapping) and delta.get("type") == "text-delta"
        if is_text_message and is_text_delta:
            first_text = not self._first_text_published and bool(delta.get("text"))
        first_reasoning = (
            is_text_message and isinstance(delta, Mapping)
            and delta.get("type") == "reasoning-delta" and bool(delta.get("reasoning"))
            and not self._first_reasoning_published
        )

        # 3.逐项判断刷新原因；后续正文与推理增量按批量阈值提交。
        flush_now = False
        if first_text or first_reasoning:
            flush_now = True  # 两种首片段分别立即交付，推理不消耗正文的首字标记。
        elif event.method != "messages":
            flush_now = True  # 工具、状态等事件立即交付，先刷出它们之前的文本。
        elif event_name in {"content-block-finish", "message-finish"}:
            flush_now = True  # 消息阶段已结束，不能把尾批留到下一阶段。
        elif len(self._pending) >= self._batch_size:
            flush_now = True  # 已达到单批数量上限。
        elif time.monotonic() - self._last_flush_at >= 0.1:
            flush_now = True  # 有新事件到达时检查时间阈值，不另建定时线程。
        # 2.首片段和阶段边界优先交付，其余事件按数量或时间阈值合并。
        if not flush_now:
            return

        # 4.先落库再通知；只有首字成功交付后才记录首字耗时。
        self._flush()
        if first_reasoning:
            self._first_reasoning_published = True
            duration_ms = (self._last_flush_at - self._started_at) * 1000
            logger.info(
                "agent_delivery_timing run_id=%s stage=first_reasoning duration_ms=%.2f",
                self._run.run_id, duration_ms,
            )
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

    def publish(self, event: WorkbenchEvent) -> None:
        """直接发布已投影的单个事件；落库、游标与通知走与批量刷新同一条路。"""
        self._pending.append(event)
        self._flush()

    def _flush(self) -> None:
        """提交失败不推进游标；通知失败也不丢弃尚未完成交付的批次。"""
        if not self._pending:
            return
        # 3.先提交数据库，成功后推进游标并唤醒订阅者。
        self._commit_events(self._run, self._coalesce_deltas(self._pending))
        self._run.last_event_sequence = self._pending[-1].seq
        self._tracker.notify_events(self._run.conversation_id, self._run.spec.invoke_from)
        self._pending.clear()
        self._last_flush_at = time.monotonic()

    @staticmethod
    def _coalesce_deltas(events: list[WorkbenchEvent]) -> list[WorkbenchEvent]:
        """只合并尚未提交的相邻同块增量；沿用最后一个序号，重连游标允许跳号。"""
        merged: list[WorkbenchEvent] = []
        for event in events:
            data = event.params.data
            delta = data.get("delta") if isinstance(data, Mapping) else None
            field = {"text-delta": "text", "reasoning-delta": "reasoning"}.get(
                delta.get("type") if isinstance(delta, Mapping) else None
            )
            if (
                merged and event.method == "messages" and field
                and data.get("event") == "content-block-delta"
                and set(delta) == {"type", field} and isinstance(delta[field], str)
            ):
                previous = merged[-1]
                previous_data = previous.params.data
                previous_delta = previous_data.get("delta") if isinstance(previous_data, Mapping) else None
                if (
                    previous.method == event.method
                    and previous.params.namespace == event.params.namespace
                    and previous.params.node == event.params.node
                    and isinstance(previous_delta, Mapping)
                    and set(previous_delta) == {"type", field}
                    and isinstance(previous_delta[field], str)
                    and previous_delta["type"] == delta["type"]
                    and {key: value for key, value in previous_data.items() if key != "delta"}
                    == {key: value for key, value in data.items() if key != "delta"}
                ):
                    combined = {**data, "delta": {**delta, field: previous_delta[field] + delta[field]}}
                    merged[-1] = event.model_copy(update={
                        "params": event.params.model_copy(update={"data": combined}),
                    })
                    continue
            merged.append(event)
        return merged


__all__ = ["AgentRunTracker", "AgentRunEventBuffer"]
