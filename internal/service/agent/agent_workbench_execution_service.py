"""工作台 Run 的 Agent 执行和结果保存。"""

from __future__ import annotations

import time
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from injector import inject
from flask import Flask, current_app

from internal.core.agent_v2 import (
    PlatformAgentRunObserver,
    AgentExecutor,
    AgentExecutionResult,
    AgentMessageError,
    ReviewBlocked,
    get_postgres_pool_stats,
    AGENT_PERSISTENCE_ERRORS,
)
from internal.service.agent_protocol.event_projector import (
    AgentProtocolEventProjector,
)
from internal.service.agent_protocol.state_projector import (
    project_execution_lifecycle,
    project_execution_state,
)
from internal.entity import WorkbenchRun, WorkbenchRunStatus
from internal.entity.conversation_entity import MessageStatus
from internal.exception import CustomException, FailException
from internal.extension.logging_extension import logger
from internal.schema.agent.agent_workbench_schema import WorkbenchEvent
from pkg.response import HttpCode
from pkg.sqlalchemy import SQLAlchemy

from .agent_assembly_service import AgentAssemblyService
from .agent_workbench_event_buffer import AgentWorkbenchEventBuffer
from .agent_workbench_event_queue import AgentWorkbenchEventQueueManager
from .agent_workbench_run_service import AgentWorkbenchRunService



@inject
@dataclass
class AgentWorkbenchRunExecutionService:
    """将 Agent 执行接入平台事件传输、业务消息和 Run 持久化。"""

    db: SQLAlchemy
    agent_assembly_service: AgentAssemblyService
    run_service: AgentWorkbenchRunService
    queue_manager: AgentWorkbenchEventQueueManager

    _RUN_WORKERS: ClassVar[int] = 4
    _run_executor: ClassVar[ThreadPoolExecutor] = ThreadPoolExecutor(max_workers=_RUN_WORKERS, thread_name_prefix="agent-workbench")
    _run_slots: ClassVar[threading.BoundedSemaphore] = threading.BoundedSemaphore(_RUN_WORKERS)

    _persistence_batch_size: ClassVar[int] = 8

    def launch(self, run: WorkbenchRun) -> None:
        """事务提交后投递到有界执行池，避免每个 Run 无限创建线程。"""
        try:
            if not self._run_slots.acquire(blocking=False):
                raise FailException("当前 Agent 执行繁忙，请稍后重试")
            self.queue_manager.register(run)
            self._run_executor.submit(
                self._execute_background_run,
                current_app._get_current_object(),
                run,
            )
        except Exception as exc:
            if self.queue_manager.get(run.conversation_id) is run:
                self._run_slots.release()
            self._fail_background_run(run, exc)
            raise

    def execute(self, run: WorkbenchRun) -> None:
        """编排执行、尾批交付和终态保存，本地登记始终由最外层清理。"""
        started_at = time.monotonic()
        observer = PlatformAgentRunObserver(run.run_id)
        projector = AgentProtocolEventProjector(
            run.run_id,
            start_sequence=run.initial_event_sequence,
            root_message_id=f"{run.spec.message_id}:assistant",
        )
        events = AgentWorkbenchEventBuffer(
            run, projector, self.run_service, self.queue_manager,
            started_at=started_at, batch_size=self._persistence_batch_size,
        )
        try:
            # 1.启动或执行失败也转成结果，后续仍需刷新尾批、保存终态。
            try:
                result = self._execute_agent(run, observer, projector, events)
            except Exception as exc:
                observer.finish()
                result = AgentExecutionResult(
                    status="failed",
                    error=exc,
                    usage=observer.build_run_usage(),
                )

            # 2.Core 已补齐消息结束分片；刷新尾批时保留原始执行错误。
            try:
                events.finish()
            except Exception as exc:
                if result.error is not None:
                    logger.exception("刷新 Agent 事件批次失败，保留原始执行错误")
                else:
                    result = replace(
                        result,
                        status="failed",
                        error=exc,
                    )

            # 3.一次提交消息、运行状态和结束事件，成功后再通知订阅者。
            self._finish_run(
                run,
                projector,
                result=result,
                latency=max(0.0, time.monotonic() - started_at),
            )
        finally:
            # 4.即使结果保存失败，也必须释放当前进程的运行登记。
            self.queue_manager.finish(run)

    def _execute_agent(
        self,
        run: WorkbenchRun,
        observer: PlatformAgentRunObserver,
        projector: AgentProtocolEventProjector,
        events: AgentWorkbenchEventBuffer,
    ) -> AgentExecutionResult:
        """集中完成启动、装配和执行；提前取消只结束本阶段，不跳过外层结果保存。"""
        # 1.写入运行状态后再次检查取消，覆盖数据库写入期间到达的取消请求。
        if not run.cancel_requested.is_set():
            self.run_service.mark_running(run)
        if run.cancel_requested.is_set():
            observer.finish()
            return AgentExecutionResult(
                status="cancelled",
                usage=observer.build_run_usage(),
            )

        # 2.先公开运行状态；恢复执行时清空待填表单，保留已提交回答。
        self._publish_event(run, projector.generated("lifecycle", {
            "event": "running",
            "phase": "preparing",
            "message_id": str(run.spec.message_id),
            "input_message_id": run.spec.input_message_id,
        }))
        if run.resume is not None:
            row = self.run_service.get_run(run.run_id)
            snapshot = {}
            if row is not None:
                snapshot = row.state_snapshot or {}
            self._publish_event(run, projector.generated("values", {
                "pending_inputs": [],
                "input_recovery": None,
                "input_history": snapshot.get("input_history", []),
            }))

        # 3.装配完成后释放查询 Session，避免模型执行期间占用数据库连接。
        try:
            agent, input_message = self.agent_assembly_service.build_agent(
                run.spec, observer=observer,
            )
        finally:
            self.db.session.close()

        # 4.取消监控使用独立线程和 Session；它启动失败时必须释放已装配资源。
        done = threading.Event()
        monitor = threading.Thread(
            target=self._watch_cancel,
            args=(current_app._get_current_object(), run, agent, done),
            daemon=True,
            name=f"agent-cancel-{run.run_id}",
        )
        try:
            self._publish_event(run, projector.generated("lifecycle", {
                "event": "running", "phase": "executing",
                "message_id": str(run.spec.message_id),
                "input_message_id": run.spec.input_message_id,
            }))
            monitor.start()
        except Exception:
            try:
                agent.close()
            except Exception:
                logger.exception("取消监控启动失败后释放 Agent 资源失败 run_id=%s", run.run_id)
            raise

        # 5.Core 根据消息或恢复答案构造框架输入，并在执行结束后释放资源。
        try:
            return agent.execute(
                message=input_message if run.resume is None else None,
                resume=run.resume,
                on_event=events.receive,
                cancel_requested=run.cancel_requested,
            )
        finally:
            # 6.停止监控，但不让阻塞的状态查询无限拖住结果落库。
            # 尚未退出的监控会在查询返回后检测 done 并释放自己的 Session。
            done.set()
            monitor.join(timeout=1)
            if monitor.is_alive():
                logger.warning("取消监控尚未退出，待当前查询返回后清理 run_id=%s", run.run_id)

    def _finish_run(
        self,
        run: WorkbenchRun,
        projector: AgentProtocolEventProjector,
        *,
        result: AgentExecutionResult,
        latency: float,
    ) -> None:
        """在同一事务中保存消息、快照和终态事件，提交后通知失败不改写结果。"""
        # 1.按执行结果确定消息状态和公开提示；未知异常详情只用于内部排障。
        status = WorkbenchRunStatus(result.status)
        message_updates, lifecycle = self._prepare_result_status(run, status, result.error)

        # 2.仅保留 UI 所需状态，待填表单与已提交回答分别存放。
        row = self.run_service.get_run(run.run_id)
        state = project_execution_state(result, row.state_snapshot if row is not None else None)

        # 3.按状态、表单请求、生命周期排序，避免客户端先收到尚未保存的表单。
        events = projector.generated_state_transition(state, lifecycle)

        # 4.回答、用量、Run 状态和事件一次提交；旧执行实例无权覆盖已收敛状态。
        if result.answer:
            message_updates["answer"] = result.answer
        message_updates["latency"] = latency
        committed = self.run_service.finalize_run(
            run,
            status,
            message_updates=message_updates,
            usage=result.usage,
            events=events,
            state_snapshot=state,
        )
        if not committed:
            return

        # 5.提交之后才推进本地游标、通知订阅者；通知失败保留已提交的业务结果。
        try:
            if events:
                run.last_event_sequence = events[-1].seq
                self.queue_manager.notify_events(run.conversation_id, run.spec.invoke_from)
        except Exception:
            logger.exception(
                "Agent 工作台终态事件发布失败 run_id=%s，保留已持久化终态",
                run.run_id,
            )

    def _prepare_result_status(
        self,
        run: WorkbenchRun,
        status: WorkbenchRunStatus,
        error: Exception | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """确定业务消息状态和公开错误，生命周期字段交给协议投影。"""
        message_updates: dict[str, Any]
        error_code = None
        if status in (WorkbenchRunStatus.WAITING_INPUT, WorkbenchRunStatus.COMPLETED):
            message_updates = {"status": MessageStatus.NORMAL}
        elif status is WorkbenchRunStatus.CANCELLED:
            message_updates = {"status": MessageStatus.STOP}
        else:
            public_error = "Agent 运行失败"
            error_code = HttpCode.FAIL.value
            if isinstance(error, CustomException):
                public_error = error.message or public_error
                error_code = error.code.value
            elif isinstance(error, AgentMessageError):
                public_error = f"模型工具调用格式错误：{error}"
            elif isinstance(error, ReviewBlocked):
                public_error = "输出内容未通过审核。"
            message_updates = {"status": MessageStatus.ERROR, "error": public_error}
            if isinstance(error, AGENT_PERSISTENCE_ERRORS):
                logger.error(
                    "Agent PostgreSQL 持久化失败 error_type=%s checkpoint_pool_stats=%s",
                    type(error).__name__,
                    get_postgres_pool_stats(),
                )
            if error is not None:
                logger.error(
                    "Agent 工作台运行失败",
                    exc_info=(type(error), error, error.__traceback__),
                )

        return message_updates, project_execution_lifecycle(
            status,
            message_id=run.spec.message_id,
            input_message_id=run.spec.input_message_id,
            public_error=message_updates.get("error"),
            error_code=error_code,
        )

    def _watch_cancel(
        self,
        flask_app: Flask,
        run: WorkbenchRun,
        agent: AgentExecutor,
        done: threading.Event,
    ) -> None:
        """独立 Session 读取跨进程取消，执行已结束时不再触碰 Agent。"""
        with flask_app.app_context():
            while not done.wait(0.5):
                try:
                    status = self.run_service.get_run_status(run.run_id)
                    if done.is_set():
                        return
                    if status == WorkbenchRunStatus.CANCELLING.value:
                        run.cancel_requested.set()
                    if run.cancel_requested.is_set() and agent.request_cancel():
                        return
                except Exception:
                    logger.exception("读取 Agent 取消状态失败 run_id=%s", run.run_id)
                finally:
                    self.db.session.remove()

    def _publish_event(
        self,
        run: WorkbenchRun,
        event: WorkbenchEvent,
    ) -> None:
        """保存单个执行事件，提交成功后推进本地游标并通知订阅者。"""
        self.run_service.record_events(run, [event])
        run.last_event_sequence = event.seq
        self.queue_manager.notify_events(run.conversation_id, run.spec.invoke_from)

    def _execute_background_run(self, flask_app: Flask, run: WorkbenchRun) -> None:
        """后台线程使用独立应用上下文和 Session，退出时兜底收敛失败状态。"""
        with flask_app.app_context():
            lease_done = threading.Event()
            lease_monitor = threading.Thread(
                target=self._watch_run_lease,
                args=(flask_app, run, lease_done),
                name=f"agent-lease-{run.run_id}",
                daemon=True,
            )
            try:
                if not self.run_service.renew_lease(run):
                    raise FailException("Agent Run 执行权已失效")
                lease_monitor.start()
                self.execute(run)
            except Exception as exc:
                self._fail_background_run(run, exc)
            finally:
                lease_done.set()
                if lease_monitor.ident is not None:
                    lease_monitor.join(timeout=1)
                self.db.session.remove()
                self._run_slots.release()

    def _watch_run_lease(
        self, flask_app: Flask, run: WorkbenchRun, done: threading.Event
    ) -> None:
        """从装配开始独立续租，模型无输出或工具耗时不会被误判为进程退出。"""
        last_renewed = time.monotonic()
        with flask_app.app_context():
            while not done.wait(10):
                try:
                    if not self.run_service.renew_lease(run):
                        run.cancel_requested.set()
                        return
                    last_renewed = time.monotonic()
                except Exception:
                    logger.exception("Agent Run 心跳更新失败 run_id=%s", run.run_id)
                    if time.monotonic() - last_renewed >= self.run_service.lease_seconds:
                        run.cancel_requested.set()
                        return
                finally:
                    self.db.session.remove()

    def _fail_background_run(self, run: WorkbenchRun, exc: Exception) -> None:
        """后台执行异常时保证数据库状态和本地队列都进入终态。"""
        event = WorkbenchEvent(
            type="event",
            event_id=f"{run.run_id}:{run.last_event_sequence + 1}",
            seq=run.last_event_sequence + 1,
            method="lifecycle",
            params={
                "data": project_execution_lifecycle(
                    WorkbenchRunStatus.FAILED,
                    message_id=run.spec.message_id,
                    input_message_id=run.spec.input_message_id,
                )
            },
        )
        try:
            committed = self.run_service.finalize_run(
                run,
                WorkbenchRunStatus.FAILED,
                message_updates={"status": "error", "error": "Agent 运行失败"},
                events=[event],
            )
            if committed:
                run.last_event_sequence = event.seq
                self.queue_manager.notify_events(run.conversation_id, run.spec.invoke_from)
        except Exception:
            logger.exception("保存 Agent Run 后台失败状态失败")
        finally:
            self.queue_manager.finish(run)
        logger.error(
            "Agent 工作台后台执行失败",
            exc_info=(type(exc), exc, exc.__traceback__),
        )




__all__ = ["AgentWorkbenchRunExecutionService"]
