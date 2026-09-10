"""工作台进程内 Run 注册、取消信号和事件提交通知。"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import UUID

from internal.entity import WorkbenchRun, WorkbenchRunStatus
from internal.entity.conversation_entity import InvokeFrom
from internal.schema.agent.agent_workbench_read_schema import WorkbenchEventBatchResp


@dataclass(frozen=True)
class _CachedEventBatch:
    """保留读取时的通知版本，提交期间形成的旧缓存不会命中新版本。"""

    version: int
    expires_at: float
    batch: WorkbenchEventBatchResp


@dataclass
class _EventSignal:
    """会话通知与短暂的共享读取；先查版本再查库可避免丢失唤醒。"""

    condition: threading.Condition = field(default_factory=threading.Condition)
    version: int = 0
    subscribers: int = 0
    read_lock: threading.Lock = field(default_factory=threading.Lock)
    batches: OrderedDict[int, _CachedEventBatch] = field(default_factory=OrderedDict)
    recovery_at: float = 0.0

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

    def read_events(
        self, cursor: int, *, load: Callable[[], WorkbenchEventBatchResp], recover: Callable[[], None],
    ) -> WorkbenchEventBatchResp:
        """同游标订阅共享已提交批次；最多保留两批 0.5 秒，通知后立即失效。"""
        with self.read_lock:
            version = self.snapshot()
            now = time.monotonic()
            cached = self.batches.get(cursor)
            if cached is not None and cached.version == version and now < cached.expires_at:
                return cached.batch
            if now >= self.recovery_at:
                recover()
                self.recovery_at = time.monotonic() + 10
            batch = load()
            self.batches[cursor] = _CachedEventBatch(version, time.monotonic() + 0.5, batch)
            self.batches.move_to_end(cursor)
            while len(self.batches) > 2:
                self.batches.popitem(last=False)
            return batch


class AgentWorkbenchEventQueueManager:
    """管理本进程的活动 Run；历史恢复由工作台 Service 负责。"""

    def __init__(self) -> None:
        """条件锁只保护本地注册表，事件正文由数据库保存。"""
        self._runs: dict[str, WorkbenchRun] = {}
        self._lock = threading.RLock()
        self._signals: dict[tuple[str, InvokeFrom], _EventSignal] = {}

    def get(
        self,
        conversation_id: UUID,
        *,
        app_id: UUID | None = None,
        invoke_from: InvokeFrom | None = None,
    ) -> WorkbenchRun | None:
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

    def register(self, run: WorkbenchRun) -> None:
        """登记当前会话的新 Run，并通知等待该会话的订阅者。"""
        with self._lock:
            self._runs[str(run.conversation_id)] = run
            self.notify_events(run.conversation_id, run.spec.invoke_from)

    def finish(self, run: WorkbenchRun) -> None:
        """结束本地 Run 并唤醒订阅者，旧 Run 不得移除后继 Run。"""
        with self._lock:
            run.done.set()
            conversation_key = str(run.conversation_id)
            if self._runs.get(conversation_key) is run:
                self._runs.pop(conversation_key)
            self.notify_events(run.conversation_id, run.spec.invoke_from)

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
                run.status = WorkbenchRunStatus.CANCELLING
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


__all__ = ["AgentWorkbenchEventQueueManager"]
