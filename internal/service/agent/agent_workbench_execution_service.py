"""工作台 Run 的 Agent 执行和结果保存。"""

from __future__ import annotations

import time
import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, ClassVar

from injector import inject
from flask import Flask, current_app
from psycopg_pool import PoolTimeout
from langgraph.types import Command
from internal.core.agent_v2.entity.user_input import PendingUserInput

from internal.core.agent_v2.observability.run_observer import (
    PlatformAgentRunObserver,
)
from internal.core.agent_v2 import AgentExecutor, AgentExecutionResult
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
from internal.schema.agent.agent_workbench_schema import WorkbenchEvent
from pkg.response import HttpCode
from pkg.sqlalchemy import SQLAlchemy

from .agent_assembly_service import AgentAssemblyService
from .agent_workbench_event_buffer import AgentWorkbenchEventBuffer
from .agent_workbench_event_queue import AgentWorkbenchEventQueueManager
from .agent_workbench_run_service import AgentWorkbenchRunService
from internal.service.conversation.conversation_service import ConversationService


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
        """编排执行、尾批交付和终态保存，本地登记始终由最外层清理。"""
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
        events = AgentWorkbenchEventBuffer(
            run, projector, observer, self.run_service, self.queue_manager,
            started_at=started_at, batch_size=self._persistence_batch_size,
        )
        try:
            # 1.启动或执行失败也转成结果，后续仍需刷新尾批、保存终态。
            try:
                result = self._execute_agent(run, observer, projector, events)
            except Exception as exc:
                observer.finish("failed", error=exc)
                result = AgentExecutionResult(
                    status="failed",
                    error=exc,
                    usage=observer.build_run_usage(),
                    observability=observer.snapshot(),
                )

            # 2.补齐消息结束分片；尾批失败不能掩盖原始执行错误。
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
                        observability={
                            **result.observability,
                            "agent_status": result.status,
                            "status": "failed",
                            "error_type": type(exc).__name__,
                        },
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
            run.status = WorkbenchRunStatus.RUNNING
            self.run_service.set_status(run, WorkbenchRunStatus.RUNNING)
        if run.cancel_requested.is_set():
            observer.finish("cancelled")
            return AgentExecutionResult(
                status="cancelled",
                usage=observer.build_run_usage(),
                observability=observer.snapshot(),
            )

        # 2.先公开运行状态；恢复执行时清空待填表单，保留已提交回答。
        self._publish_event(run, projector.generated("lifecycle", {
            "event": "running",
            "message_id": str(run.spec.message_id),
            "input_message_id": run.spec.input_message_id,
        }))
        if run.resume is not None:
            row = self.run_service.get_run(run.run_id, recover=False)
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
            monitor.start()
        except Exception:
            try:
                agent.close()
            except Exception:
                logger.exception("取消监控启动失败后释放 Agent 资源失败 run_id=%s", run.run_id)
            raise

        # 5.恢复命令与首次消息分别构造；Core 负责执行结束后的 Agent 资源释放。
        try:
            if run.resume is not None:
                graph_input = Command(resume=run.resume)
            else:
                graph_input = {"messages": [input_message]}
            return agent.execute(
                graph_input,
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
        error = result.error
        message_updates: dict[str, Any]
        lifecycle: dict[str, Any]
        if status is WorkbenchRunStatus.WAITING_INPUT:
            message_updates = {"status": MessageStatus.NORMAL}
            lifecycle = {"event": "interrupted", "status": "waiting_input", "reason": "user_input_required"}
        elif status is WorkbenchRunStatus.COMPLETED:
            message_updates = {"status": MessageStatus.NORMAL}
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
            run.error_message = str(error) if error is not None else ""
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

        # 2.仅保留 UI 所需状态，待填表单与已提交回答分别存放。
        public_state = {}
        for key in ("todos", "files", "async_tasks"):
            if key in result.state:
                public_state[key] = result.state[key]
        state = project_public_state(public_state)
        pending = []
        for item in result.interrupts:
            pending_input = PendingUserInput.model_validate(item)
            pending.append(pending_input.model_dump(mode="json"))
        row = self.run_service.get_run(run.run_id, recover=False)
        snapshot = {}
        if row is not None:
            snapshot = row.state_snapshot or {}
        state.update(
            pending_inputs=pending,
            input_history=list(snapshot.get("input_history", [])),
            input_recovery=None,
        )

        # 3.按状态、表单请求、生命周期排序，避免客户端先收到尚未保存的表单。
        raw_events = [projector.generated("values", state)]
        for item in pending:
            raw_events.append(projector.generated("input.requested", {
                "interrupt_id": item["interrupt_id"], "payload": item["payload"],
            }))
        lifecycle.update(
            message_id=str(run.spec.message_id),
            input_message_id=run.spec.input_message_id,
        )
        raw_events.append(projector.generated("lifecycle", lifecycle))
        events = []
        for raw in raw_events:
            event = AgentProtocolEventProjector.to_public_event(raw)
            if event is not None:
                events.append(event)

        # 4.回答、用量、Run 状态和事件一次提交；旧执行实例无权覆盖已收敛状态。
        if result.answer:
            message_updates["answer"] = result.answer
        message_updates["latency"] = latency
        message_updates.update(ConversationService.build_run_usage_updates(result.usage))
        committed = self.run_service.finalize_run(
            run,
            status,
            error_message=str(result.error) if result.error is not None else "",
            message_updates=message_updates,
            observability=result.observability,
            events=events,
            state_snapshot=state,
        )
        if not committed:
            return
        run.status = status

        # 5.提交之后才推进本地游标、通知订阅者；通知失败保留已提交的业务结果。
        try:
            for event in events:
                self._publish_event(run, event, persist_database=False)
        except Exception:
            logger.exception(
                "Agent 工作台终态事件发布失败 run_id=%s，保留已持久化终态",
                run.run_id,
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
                    if run.cancel_requested.is_set() and agent.request_drain():
                        return
                except Exception:
                    logger.exception("读取 Agent 取消状态失败 run_id=%s", run.run_id)
                finally:
                    self.db.session.remove()

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
        self.queue_manager.notify_events(run.conversation_id, run.spec.invoke_from)
        return event


__all__ = ["AgentWorkbenchRunExecutionService"]
