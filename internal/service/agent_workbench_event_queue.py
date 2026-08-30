"""工作台本地事件缓冲管理器。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from uuid import UUID

from internal.entity import WorkbenchRun, WorkbenchRunStatus
from internal.schema.agent_workbench_schema import WorkbenchEvent


@dataclass(frozen=True)
class WorkbenchLocalEventBatch:
    """一次本地队列读取的结果。"""

    events: tuple[WorkbenchEvent, ...]
    oldest_event_sequence: int | None
    timed_out: bool
    finished: bool


@dataclass
class AgentWorkbenchEventQueue:
    """管理本进程 Run 注册表、事件缓冲和等待通知。"""

    _runs: dict[str, WorkbenchRun] = field(init=False, default_factory=dict)
    _condition: threading.Condition = field(
        init=False,
        default_factory=threading.Condition,
    )

    @property
    def condition(self) -> threading.Condition:
        """返回共享条件变量，供启动去重逻辑保持原子性。"""
        return self._condition

    def wait(self, timeout: float | None) -> None:
        """等待本地 Run 或事件状态变化。"""
        with self._condition:
            self._condition.wait(timeout=timeout)

    def notify_all(self) -> None:
        """唤醒等待本地 Run 状态变化的订阅者。"""
        with self._condition:
            self._condition.notify_all()

    def get(self, thread_id: UUID | str) -> WorkbenchRun | None:
        """读取线程对应的本地活动 Run。"""
        with self._condition:
            return self._runs.get(str(thread_id))

    def register(self, run: WorkbenchRun) -> None:
        """注册本地 Run，并唤醒等待中的订阅者。"""
        with self._condition:
            self._runs[str(run.thread_id)] = run
            self._condition.notify_all()

    def finish(self, run: WorkbenchRun) -> None:
        """标记本地 Run 完成并移出活动注册表。"""
        with self._condition:
            run.done.set()
            key = str(run.thread_id)
            if self._runs.get(key) is run:
                self._runs.pop(key, None)
            self._condition.notify_all()

    def publish(self, run: WorkbenchRun, event: WorkbenchEvent) -> None:
        """追加事件到 Run 的有界缓冲并唤醒订阅者。"""
        with self._condition:
            run.events.append(event)
            self._condition.notify_all()

    def request_cancel(self, thread_id: UUID | str, run_id: UUID) -> bool:
        """向本进程内匹配的 Run 设置取消信号。"""
        with self._condition:
            run = self._runs.get(str(thread_id))
            matched = run is not None and run.run_id == run_id
            if matched:
                run.cancel_requested.set()
                run.status = WorkbenchRunStatus.CANCELLING
            self._condition.notify_all()
            return matched

    def read(
        self,
        run: WorkbenchRun,
        *,
        cursor: int,
        timeout: float | None,
    ) -> WorkbenchLocalEventBatch:
        """等待并读取游标之后的本地事件，同时判断队列是否已结束。"""
        with self._condition:
            ready = self._condition.wait_for(
                lambda: run.done.is_set()
                or any(event.seq > cursor for event in run.events),
                timeout=timeout,
            )
            oldest_event_sequence = run.events[0].seq if run.events else None
            pending = tuple(event for event in run.events if event.seq > cursor)
            return WorkbenchLocalEventBatch(
                events=pending,
                oldest_event_sequence=oldest_event_sequence,
                timed_out=not ready,
                finished=run.done.is_set() and not pending,
            )


__all__ = ["AgentWorkbenchEventQueue", "WorkbenchLocalEventBatch"]
