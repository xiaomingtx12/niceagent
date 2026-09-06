"""工作台 Run 的入队、去重、启动和取消协调。"""

from __future__ import annotations

import threading
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
from internal.extension.logging_extension import logger
from internal.model import AgentRun
from internal.repository import AgentWorkbenchRedisRepository
from internal.schema.agent_workbench_schema import (
    WorkbenchCommandErrorResp,
    WorkbenchCommandSuccessResp,
)
from pkg.sqlalchemy import SQLAlchemy

from .agent_workbench_event_queue import AgentWorkbenchEventQueueManager
from .agent_workbench_execution_service import AgentWorkbenchRunExecutionService
from .agent_workbench_run_service import AgentWorkbenchRunService


@inject
@dataclass
class AgentWorkbenchRunCoordinator:
    """协调 Run 的并发、持久化、后台启动和取消，不参与 Agent 事件处理。"""

    db: SQLAlchemy
    run_service: AgentWorkbenchRunService
    redis_repository: AgentWorkbenchRedisRepository
    queue_manager: AgentWorkbenchEventQueueManager
    execution_service: AgentWorkbenchRunExecutionService

    _starting_runs: dict[str, WorkbenchRunStartReservation] = field(
        init=False,
        default_factory=dict,
    )
    _predecessor_poll_interval: ClassVar[float] = 0.25

    # -------------------- Public coordination API --------------------

    def enqueue(
        self,
        *,
        conversation_id: UUID,
        invoke_from: InvokeFrom,
        input_message_id: str | None,
        run_builder: Callable[..., WorkbenchRun],
        command_id: int | None = None,
    ) -> WorkbenchCommandSuccessResp | WorkbenchCommandErrorResp:
        """执行 Run 去重、入库、注册和后台启动。"""
        key = str(conversation_id)
        reservation = WorkbenchRunStartReservation(uuid4(), input_message_id)
        previous_run: WorkbenchRun | None = None

        # 1.先检查本进程状态，快速处理重复提交和同会话并发冲突。
        with self.queue_manager.condition:
            active = self.queue_manager.get(key)
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
        run: WorkbenchRun | None = None
        try:
            # 2.在数据库事务中加会话锁，再执行幂等查询和 Run 入库。
            with self.db.auto_commit():
                self.run_service.lock_conversation_start(conversation_id)
                duplicate_run, persisted_active = self.run_service.get_start_candidates(
                    conversation_id=conversation_id,
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
                    run = run_builder(run_id=reservation.run_id)
                    persisted_sequence = (
                        self.run_service.get_conversation_last_event_sequence(
                            conversation_id=conversation_id,
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

        if run is None:
            # 仅用于保护后续代码的状态不变量；正常分支一定会创建 Run。
            self._clear_start_reservation(key, reservation)
            raise RuntimeError("工作台 Run 创建结果为空")

        with self.queue_manager.condition:
            if self._starting_runs.get(key) is reservation:
                self._starting_runs.pop(key, None)
            self.queue_manager.register(run)
        # 3.启动带应用上下文的后台执行线程。
        try:
            self._start_background_run(
                run,
                wait_for=previous_run,
                wait_for_persisted_predecessor=(
                    previous_run is None and persisted_active is not None
                ),
            )
        except Exception as exc:
            self._fail_background_start(run, exc)
            raise
        return WorkbenchCommandSuccessResp.create(
            command_id=command_id,
            run_id=run.run_id,
            applied_through_sequence=applied_through_sequence,
        )

    def request_cancel(self, run: AgentRun, *, conversation_id: UUID) -> bool:
        """持久化并广播取消请求，不负责权限校验。"""
        # 1.先用数据库活动状态判定取消是否仍然有效。
        if run.status in WORKBENCH_TERMINAL_STATUSES:
            return False
        if not self.run_service.request_cancel(run):
            # 取消请求和终态收敛可能并发发生，以数据库实际状态为准。
            return False
        # 2.通知跨进程 Redis 和当前 Worker 的本地队列。
        if not self.redis_repository.request_cancel(run.id):
            logger.warning(
                "Agent 工作台取消信号 Redis 广播失败 run_id=%s",
                run.id,
            )
        self.queue_manager.request_cancel(conversation_id, run.id)
        return True

    # -------------------- Internal startup helpers --------------------

    def _clear_start_reservation(
        self,
        key: str,
        reservation: WorkbenchRunStartReservation,
    ) -> None:
        """清理启动占位并唤醒等待同一会话的提交。"""
        with self.queue_manager.condition:
            if self._starting_runs.get(key) is reservation:
                self._starting_runs.pop(key, None)
            self.queue_manager.condition.notify_all()

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
                    # 同一会话禁止并发写 LangGraph checkpoint。
                    if wait_for_persisted_predecessor:
                        while self.run_service.has_unfinished_predecessor(run.run_id):
                            # 本进程前置 Run 完成时由队列立即唤醒，跨进程仍按间隔兜底。
                            self.queue_manager.wait(self._predecessor_poll_interval)
                    if wait_for is not None or wait_for_persisted_predecessor:
                        persisted_sequence = (
                            self.run_service.get_conversation_last_event_sequence(
                                conversation_id=run.conversation_id,
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
            logger.exception("保存 Agent Run 初始化失败状态失败")
        finally:
            self.queue_manager.finish(run)
        logger.error(
            "Agent 工作台后台初始化失败",
            exc_info=(type(exc), exc, exc.__traceback__),
        )

__all__ = ["AgentWorkbenchRunCoordinator"]
