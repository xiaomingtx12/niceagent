"""工作台 Run 的入队、去重、启动和取消协调。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar
from uuid import UUID, uuid4

from flask import current_app
from injector import inject

from internal.entity import (
    WORKBENCH_TERMINAL_STATUSES,
    WorkbenchRun,
    WorkbenchRunStartReservation,
    WorkbenchRunStatus,
)
from internal.entity.conversation_entity import InvokeFrom
from internal.model import AgentRun
from internal.repository import AgentWorkbenchRedisRepository
from internal.schema.agent_workbench_schema import (
    WorkbenchCommandErrorResp,
    WorkbenchCommandSuccessResp,
)
from pkg.sqlalchemy import SQLAlchemy

from .agent_workbench_event_queue import AgentWorkbenchEventQueue
from .agent_workbench_execution_service import AgentWorkbenchRunExecutionService
from .agent_workbench_run_service import AgentWorkbenchRunService


@inject
@dataclass
class AgentWorkbenchRunCoordinator:
    """协调 Run 的并发、持久化、后台启动和取消，不参与 Agent 事件处理。"""

    db: SQLAlchemy
    run_service: AgentWorkbenchRunService
    redis_repository: AgentWorkbenchRedisRepository
    event_queue: AgentWorkbenchEventQueue
    execution_service: AgentWorkbenchRunExecutionService

    _starting_runs: dict[str, WorkbenchRunStartReservation] = field(
        init=False,
        default_factory=dict,
    )
    _cancel_signal_poll_interval: ClassVar[float] = 0.25

    def enqueue(
        self,
        *,
        thread_id: UUID,
        invoke_from: InvokeFrom,
        input_message_id: str | None,
        command_id: int | None,
        create_run: Callable[[UUID], WorkbenchRun],
    ) -> WorkbenchCommandSuccessResp | WorkbenchCommandErrorResp:
        """执行 Run 去重、入库、注册和后台启动。"""
        key = str(thread_id)
        reservation = WorkbenchRunStartReservation(uuid4(), input_message_id)
        previous_run: WorkbenchRun | None = None

        # 1.先检查本进程状态，快速处理重复提交和同线程并发冲突。
        with self.event_queue.condition:
            active = self.event_queue.get(key)
            if (
                active is not None
                and input_message_id is not None
                and active.spec.input_message_id == input_message_id
            ):
                return WorkbenchCommandSuccessResp.create(
                    command_id=command_id,
                    run_id=active.run_id,
                    applied_through_sequence=active.initial_event_sequence,
                )
            if active is not None and not active.done.is_set():
                # 取消请求是异步收敛的；等待前置 Run 真正结束后再执行下一轮。
                if (
                    active.cancel_requested.is_set()
                    or active.status is WorkbenchRunStatus.CANCELLING
                ):
                    previous_run = active
                else:
                    return WorkbenchCommandErrorResp(
                        id=command_id,
                        message="当前任务已有正在执行的 Agent",
                    )
            starting = self._starting_runs.get(key)
            if starting is not None:
                if (
                    input_message_id is not None
                    and starting.input_message_id == input_message_id
                ):
                    return WorkbenchCommandSuccessResp.create(
                        command_id=command_id,
                        run_id=starting.run_id,
                    )
                return WorkbenchCommandErrorResp(
                    id=command_id,
                    message="当前任务已有正在启动的 Agent",
                )
            self._starting_runs[key] = reservation

        conflict_message: str | None = None
        duplicate_run: AgentRun | None = None
        try:
            # 2.在数据库事务中加线程锁，再执行幂等查询和 Run 入库。
            with self.db.auto_commit():
                self.run_service.lock_thread_start(thread_id)
                duplicate_run, persisted_active = self.run_service.get_start_candidates(
                    thread_id=thread_id,
                    input_message_id=input_message_id,
                )
                if duplicate_run is not None:
                    applied_through_sequence = int(
                        duplicate_run.last_event_sequence or 0
                    )
                elif (
                    persisted_active is not None
                    and persisted_active.status != WorkbenchRunStatus.CANCELLING.value
                ):
                    conflict_message = "当前任务已有正在执行的 Agent"
                else:
                    run = create_run(reservation.run_id)
                    persisted_sequence = (
                        self.run_service.get_thread_last_event_sequence(
                            thread_id=thread_id,
                            invoke_from=invoke_from,
                            close_session=False,
                        )
                    )
                    local_sequence = (
                        int(previous_run.last_event_sequence or 0)
                        if previous_run is not None
                        else 0
                    )
                    applied_through_sequence = max(
                        persisted_sequence,
                        local_sequence,
                    )
                    run.initial_event_sequence = applied_through_sequence
                    run.last_event_sequence = applied_through_sequence
                    self.run_service.add_run(run)
        except Exception:
            self._clear_start_reservation(key, reservation)
            raise

        if conflict_message is not None:
            self._clear_start_reservation(key, reservation)
            return WorkbenchCommandErrorResp(
                id=command_id,
                message=conflict_message,
            )

        if duplicate_run is not None:
            self._clear_start_reservation(key, reservation)
            return WorkbenchCommandSuccessResp.create(
                command_id=command_id,
                run_id=duplicate_run.id,
                applied_through_sequence=applied_through_sequence,
            )

        with self.event_queue.condition:
            if self._starting_runs.get(key) is reservation:
                self._starting_runs.pop(key, None)
            self.event_queue.register(run)
        # 3.通知跨进程 SSE 订阅者有新 Run；Redis 故障时仍由本进程队列唤醒。
        self.redis_repository.publish_run_started(
            thread_id=thread_id,
            run_id=run.run_id,
        )
        try:
            self._start_background_run(
                run,
                wait_for=previous_run,
                wait_for_persisted_predecessor=(
                    previous_run is None and persisted_active is not None
                ),
            )
        except Exception as exc:
            run.status = WorkbenchRunStatus.FAILED
            run.error_message = str(exc)
            self.run_service.set_status(
                run,
                WorkbenchRunStatus.FAILED,
                error_message=str(exc),
            )
            self.event_queue.finish(run)
            raise
        return WorkbenchCommandSuccessResp.create(
            command_id=command_id,
            run_id=run.run_id,
            applied_through_sequence=applied_through_sequence,
        )

    def _clear_start_reservation(
        self,
        key: str,
        reservation: WorkbenchRunStartReservation,
    ) -> None:
        """清理启动占位并唤醒等待同一线程的提交。"""
        with self.event_queue.condition:
            if self._starting_runs.get(key) is reservation:
                self._starting_runs.pop(key, None)
            self.event_queue.notify_all()

    def _start_background_run(
        self,
        run: WorkbenchRun,
        *,
        wait_for: WorkbenchRun | None = None,
        wait_for_persisted_predecessor: bool = False,
    ) -> None:
        """启动带 Flask application context 的后台生产线程。"""
        flask_app = current_app._get_current_object()

        def execute() -> None:
            with flask_app.app_context():
                try:
                    if wait_for is not None:
                        wait_for.done.wait()
                    # 同一 LangGraph thread 禁止并发写 checkpoint。
                    if wait_for_persisted_predecessor:
                        while self.run_service.has_unfinished_predecessor(run.run_id):
                            time.sleep(self._cancel_signal_poll_interval)
                    if wait_for is not None or wait_for_persisted_predecessor:
                        persisted_sequence = (
                            self.run_service.get_thread_last_event_sequence(
                                thread_id=run.thread_id,
                                invoke_from=run.spec.invoke_from,
                            )
                        )
                        run.initial_event_sequence = max(
                            run.initial_event_sequence,
                            persisted_sequence,
                        )
                        run.last_event_sequence = run.initial_event_sequence
                    self.execution_service.execute(run)
                except Exception as exc:
                    self._fail_background_start(run, exc)
                finally:
                    self.db.session.remove()

        thread = threading.Thread(
            target=execute,
            name=f"agent-workbench-{run.run_id}",
            daemon=True,
        )
        thread.start()

    def _fail_background_start(self, run: WorkbenchRun, exc: Exception) -> None:
        """处理后台初始化失败，保证数据库和本地状态都能结束。"""
        run.status = WorkbenchRunStatus.FAILED
        run.error_message = str(exc)
        try:
            self.run_service.set_status(
                run,
                WorkbenchRunStatus.FAILED,
                error_message=str(exc),
            )
        except Exception:
            current_app.logger.exception("保存 Agent Run 初始化失败状态失败")
        finally:
            self.event_queue.finish(run)
        current_app.logger.exception(
            "Agent 工作台后台初始化失败",
            exc_info=(type(exc), exc, exc.__traceback__),
        )

    def request_cancel(self, run: AgentRun, *, thread_id: UUID) -> bool:
        """持久化并广播取消请求，不负责权限校验。"""
        if run.status in WORKBENCH_TERMINAL_STATUSES:
            return False
        self.run_service.request_cancel(run)
        self.redis_repository.request_cancel(run.id)
        self.event_queue.request_cancel(thread_id, run.id)
        return True


__all__ = ["AgentWorkbenchRunCoordinator"]
