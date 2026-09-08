"""工作台进程内 Run 注册、取消信号和事件提交通知。"""

from __future__ import annotations

import threading
from uuid import UUID

from internal.entity import WorkbenchRun, WorkbenchRunStatus
from internal.entity.conversation_entity import InvokeFrom


class AgentWorkbenchEventQueueManager:
    """管理本进程的活动 Run；历史恢复由工作台 Service 负责。"""

    def __init__(self) -> None:
        """条件锁只保护本地注册表，事件正文由数据库保存。"""
        self._runs: dict[str, WorkbenchRun] = {}
        self._condition = threading.Condition()

    def get(
        self,
        conversation_id: UUID,
        *,
        app_id: UUID | None = None,
        invoke_from: InvokeFrom | None = None,
    ) -> WorkbenchRun | None:
        """读取会话的活动 Run，入口不匹配时视为不存在。"""
        with self._condition:
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
        with self._condition:
            self._runs[str(run.conversation_id)] = run
            self._condition.notify_all()

    def finish(self, run: WorkbenchRun) -> None:
        """结束本地 Run 并唤醒订阅者，旧 Run 不得移除后继 Run。"""
        with self._condition:
            run.done.set()
            conversation_key = str(run.conversation_id)
            if self._runs.get(conversation_key) is run:
                self._runs.pop(conversation_key)
            self._condition.notify_all()

    def notify_events(self) -> None:
        """已提交事件后唤醒本进程订阅者，不重复保存事件正文。"""
        with self._condition:
            self._condition.notify_all()

    def request_cancel(
        self,
        conversation_id: UUID,
        run_id: UUID,
        *,
        app_id: UUID | None = None,
        invoke_from: InvokeFrom | None = None,
    ) -> bool:
        """只通知本进程内的目标 Run，数据库取消状态由调用方负责。"""
        with self._condition:
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
                self._condition.notify_all()
            return True

    def wait_for_events(self, *, timeout: float) -> None:
        """本进程事件唤醒订阅者；超时后查询数据库以覆盖其他进程的事件。"""
        with self._condition:
            self._condition.wait(timeout=timeout)


__all__ = ["AgentWorkbenchEventQueueManager"]
