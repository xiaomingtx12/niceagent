"""工作台 Run 的 Agent 执行和结果保存。"""

from __future__ import annotations

import time
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from injector import inject
from flask import current_app
from psycopg_pool import PoolTimeout
from langgraph.types import Command
from internal.core.agent_v2.entity.user_input import PendingUserInput

from internal.core.agent_v2.observability.run_observer import (
    PlatformAgentRunObserver,
)
from internal.core.agent_v2 import AgentEvent, AgentExecutionResult
from internal.core.agent_v2.middleware.tool_call_validation_middleware import (
    AgentMessageError,
)
from internal.core.agent_v2.middleware.review_middleware import ReviewBlocked
from internal.service.agent_protocol.event_projector import (
    AgentProtocolEventProjector,
    project_public_state,
)
from internal.core.agent_v2.persistence.langgraph_checkpoint_provider import (
    get_postgres_pool_stats,
)
from internal.core.agent_v2.persistence.postgres_connection import (
    POSTGRES_CONNECTION_ERRORS,
)
from internal.entity import WorkbenchRun, WorkbenchRunStatus
from internal.entity.conversation_entity import MessageStatus
from internal.exception import CustomException
from internal.extension.logging_extension import logger
from internal.schema.agent_workbench_schema import WorkbenchEvent
from pkg.response import HttpCode
from pkg.sqlalchemy import SQLAlchemy

from .agent_assembly_service import AgentAssemblyService
from .agent_workbench_event_queue import AgentWorkbenchEventQueueManager
from .agent_workbench_run_service import AgentWorkbenchRunService
from .conversation_service import ConversationService


_AGENT_PERSISTENCE_ERRORS = (*POSTGRES_CONNECTION_ERRORS, PoolTimeout)


@inject
@dataclass
class AgentWorkbenchRunExecutionService:
    """将 Agent 执行接入平台事件传输、业务消息和 Run 持久化。"""

    db: SQLAlchemy
    agent_assembly_service: AgentAssemblyService
    run_service: AgentWorkbenchRunService
    queue_manager: AgentWorkbenchEventQueueManager

    _persistence_batch_size: ClassVar[int] = 8

    def execute(self, run: WorkbenchRun) -> None:
        """创建平台运行上下文，接收 Agent 事件并保存其执行结果。"""
        started_at = time.monotonic()
        observer = PlatformAgentRunObserver(
            run.run_id,
            attributes={
                "account_id": str(run.spec.account_id),
                "app_id": str(run.spec.app_id),
                "conversation_id": str(run.spec.conversation_id),
                "invoke_from": run.spec.invoke_from.value,
            },
        )
        projector = AgentProtocolEventProjector(
            run.run_id,
            start_sequence=run.initial_event_sequence,
            root_message_id=f"{run.spec.message_id}:assistant",
        )
        pending_events: list[WorkbenchEvent] = []
        last_flush_at = time.monotonic()

        def receive_event(agent_event: AgentEvent) -> None:
            """批量提交所有通道后再通知订阅者，确保实时与重放使用相同事件。"""
            nonlocal last_flush_at
            for projected in projector.project_many(agent_event):
                event = AgentProtocolEventProjector.to_public_event(projected)
                if event is None:
                    continue
                pending_events.append(event)
                if (
                    len(pending_events) >= self._persistence_batch_size
                    or time.monotonic() - last_flush_at >= 0.1
                    # 阶段结束和工具启动前必须刷新，不能把尾批留到慢工具返回。
                    or event.method != "messages"
                    or event.params.data.get("event") in {
                        "content-block-finish", "message-finish"
                    }
                ):
                    self.run_service.record_events(run, pending_events)
                    for committed in pending_events:
                        self._publish_event(run, committed, persist_database=False)
                    pending_events.clear()
                    last_flush_at = time.monotonic()

        try:
            # 1.先更新平台运行状态，再装配 Agent，避免取消后重复创建运行资源。
            try:
                if not run.cancel_requested.is_set():
                    run.status = WorkbenchRunStatus.RUNNING
                    self.run_service.set_status(run, WorkbenchRunStatus.RUNNING)
                if not run.cancel_requested.is_set():
                    self._publish_event(
                        run,
                        projector.generated(
                            "lifecycle",
                            {
                                "event": "running",
                                "message_id": str(run.spec.message_id),
                                "input_message_id": run.spec.input_message_id,
                            },
                        ),
                    )
                    if run.resume is not None:
                        row = self.run_service.get_run(run.run_id, recover=False)
                        snapshot = row.state_snapshot if row is not None else {}
                        self._publish_event(run, projector.generated("values", {
                            "pending_inputs": [], "input_history": (snapshot or {}).get("input_history", []),
                        }))
                    try:
                        agent, input_message = self.agent_assembly_service.build_agent(
                            run.spec,
                            observer=observer,
                        )
                    finally:
                        self.db.session.close()

                    # 2.Agent 负责执行控制和资源释放，平台负责协议适配。
                    monitor_done = threading.Event()
                    flask_app = current_app._get_current_object()

                    def watch_cancel() -> None:
                        """独立 Session 读取跨进程取消，模型无输出时也能请求协作退出。"""
                        with flask_app.app_context():
                            while not monitor_done.wait(0.5):
                                try:
                                    row = self.run_service.get_run(run.run_id, recover=False)
                                    if row is not None and row.status == "cancelling":
                                        run.cancel_requested.set()
                                    if run.cancel_requested.is_set():
                                        if agent.request_drain():
                                            return
                                except Exception:
                                    logger.exception(
                                        "读取 Agent 取消状态失败 run_id=%s", run.run_id
                                    )
                                finally:
                                    self.db.session.remove()

                    monitor = threading.Thread(
                        target=watch_cancel,
                        daemon=True,
                        name=f"agent-cancel-{run.run_id}",
                    )
                    try:
                        monitor.start()
                    except Exception:
                        agent.close()
                        raise
                    try:
                        result = agent.execute(
                            Command(resume=run.resume) if run.resume is not None else {"messages": [input_message]},
                            on_event=receive_event,
                            cancel_requested=run.cancel_requested,
                        )
                    finally:
                        monitor_done.set()
                        if monitor.ident is not None:
                            monitor.join()
                else:
                    observer.finish("cancelled")
                    result = AgentExecutionResult(
                        status="cancelled",
                        usage=observer.build_run_usage(),
                        observability=observer.snapshot(),
                    )
            except Exception as exc:
                # 尚未拿到执行结果时，平台兜底记录启动或结果交付失败。
                observer.finish("failed", error=exc)
                result = AgentExecutionResult(
                    status="failed",
                    error=exc,
                    usage=observer.build_run_usage(),
                    observability=observer.snapshot(),
                )

            # 3.补齐工作台消息分片，再刷新平台历史尾批。
            try:
                for raw in projector.finish_pending_messages():
                    event = AgentProtocolEventProjector.to_public_event(raw)
                    if event is not None:
                        pending_events.append(event)
                if pending_events:
                    self.run_service.record_events(run, pending_events)
                    for event in pending_events:
                        self._publish_event(run, event, persist_database=False)
                    pending_events.clear()
            except Exception as exc:
                if result.error is not None:
                    logger.exception("刷新 Agent 事件批次失败，保留原始执行错误")
                else:
                    result = replace(
                        result,
                        status="failed",
                        error=exc,
                        observability={
                            **result.observability,
                            "agent_status": result.status,
                            "status": "failed",
                            "error_type": type(exc).__name__,
                        },
                    )

            # 4.把 Agent 结果映射到业务消息和 Run，成功落库后才发送平台终态。
            self._finish_run(
                run,
                projector,
                result=result,
                latency=max(0.0, time.monotonic() - started_at),
            )
        finally:
            self.queue_manager.finish(run)

    def _finish_run(
        self,
        run: WorkbenchRun,
        projector: AgentProtocolEventProjector,
        *,
        result: AgentExecutionResult,
        latency: float,
    ) -> None:
        """保存消息和 Run 终态后再发结束事件，事件发送失败不能改写已保存结果。"""
        # 1.将 Agent 结果映射为平台消息状态和对外终态。
        status = WorkbenchRunStatus(result.status)
        error = result.error
        message_updates: dict[str, Any]
        lifecycle: dict[str, Any]
        error_message = str(error) if error is not None else ""
        if status is WorkbenchRunStatus.WAITING_INPUT:
            message_updates = {"status": MessageStatus.NORMAL}
            lifecycle = {"event": "interrupted", "status": "waiting_input", "reason": "user_input_required"}
        elif status is WorkbenchRunStatus.COMPLETED:
            message_updates = {"status": MessageStatus.NORMAL, "latency": latency}
            if result.answer:
                message_updates["answer"] = result.answer
            lifecycle = {"event": "completed", "status": "completed"}
        elif status is WorkbenchRunStatus.CANCELLED:
            message_updates = {"status": MessageStatus.STOP}
            lifecycle = {
                "event": "interrupted",
                "status": "cancelled",
                "reason": "cancel_requested",
            }
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
            lifecycle = {
                "event": "failed",
                "status": "failed",
                "code": error_code,
                "error": public_error,
            }
            run.error_message = error_message
            if isinstance(error, _AGENT_PERSISTENCE_ERRORS):
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

        # 2.平台只保存 UI 所需的状态字段，消息正文已通过结果单独获得。
        state = project_public_state(
            {
                key: result.state[key]
                for key in ("todos", "files", "async_tasks")
                if key in result.state
            }
        )
        pending = [PendingUserInput.model_validate(item).model_dump(mode="json") for item in result.interrupts]
        row = self.run_service.get_run(run.run_id, recover=False)
        history = list((row.state_snapshot or {}).get("input_history", [])) if row is not None else []
        state.update(pending_inputs=pending, input_history=history)
        # 表单及等待状态在同一事务提交，避免浏览器收到尚不可提交的表单。
        final_events = [AgentProtocolEventProjector.to_public_event(projector.generated("values", state))]
        final_events.extend(
            AgentProtocolEventProjector.to_public_event(projector.generated(
                "input.requested", {"interrupt_id": item["interrupt_id"], "payload": item["payload"]},
            )) for item in pending
        )

        # 3.同一事务保存消息、Agent 用量和平台终态。
        if result.answer:
            message_updates["answer"] = result.answer
        message_updates["latency"] = latency
        lifecycle.update(
            message_id=str(run.spec.message_id),
            input_message_id=run.spec.input_message_id,
        )
        terminal_event = AgentProtocolEventProjector.to_public_event(
            projector.generated("lifecycle", lifecycle)
        )
        message_updates.update(
            ConversationService.build_run_usage_updates(result.usage),
        )
        committed = self.run_service.finalize_run(
            run,
            status,
            error_message=error_message,
            message_updates=message_updates,
            observability=result.observability,
            events=[event for event in [*final_events, terminal_event] if event is not None],
            state_snapshot=state,
        )
        if not committed:
            return
        run.status = status

        # 4.结束事件保存失败只记录日志，不能把成功或取消改成失败。
        try:
            for event in [*final_events, terminal_event]:
                if event is not None:
                    self._publish_event(run, event, persist_database=False)
        except Exception:
            logger.exception(
                "Agent 工作台终态事件发布失败 run_id=%s，保留已持久化终态",
                run.run_id,
            )

    def _publish_event(
        self,
        run: WorkbenchRun,
        raw_event: Mapping[str, Any] | WorkbenchEvent,
        *,
        persist_database: bool = True,
    ) -> WorkbenchEvent | None:
        """统一公开事件投影、内存更新和发布顺序；批量消费时由调用方落库。"""
        event = AgentProtocolEventProjector.to_public_event(raw_event)
        if event is None:
            return None

        if persist_database:
            self.run_service.record_events(run, [event])

        run.last_event_sequence = event.seq
        self.queue_manager.notify_events()
        return event


__all__ = ["AgentWorkbenchRunExecutionService"]
