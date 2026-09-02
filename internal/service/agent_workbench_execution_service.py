"""工作台 Run 的 Agent 执行和终态收敛。"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from flask import current_app
from injector import inject
from langgraph.errors import GraphDrained
from langchain_core.language_models import BaseChatModel
from pydantic import ValidationError
from psycopg_pool import PoolTimeout

from internal.core.agent_v2.protocol.event_projector import (
    AgentProtocolEventProjector,
)
from internal.core.agent_v2.protocol.message_parser import message_content_to_text
from internal.core.agent_v2.persistence.langgraph_checkpoint_provider import (
    get_postgres_pool_stats,
)
from internal.core.agent_v2.persistence.postgres_connection import (
    POSTGRES_CONNECTION_ERRORS,
)
from internal.entity import WorkbenchRun, WorkbenchRunStatus
from internal.entity.conversation_entity import InvokeFrom, MessageStatus
from internal.exception import (
    AgentInvocationException,
    NotFoundException,
    ValidateErrorException,
)
from internal.model import App
from internal.repository import AgentWorkbenchRedisRepository
from internal.schema.agent_attachment_schema import dump_agent_attachments
from internal.schema.agent_workbench_schema import WorkbenchEvent
from internal.schema.app_config_schema import AgentAppConfigReq
from pkg.sqlalchemy import SQLAlchemy

from .agent_assembly_service import AgentAssemblyService
from .agent_workbench_event_queue import AgentWorkbenchEventQueue
from .agent_workbench_run_service import AgentWorkbenchRunService
from .app_config_service import AppConfigService
from .base_service import BaseService
from .conversation_service import ConversationService


_AGENT_PERSISTENCE_ERRORS = (*POSTGRES_CONNECTION_ERRORS, PoolTimeout)


@inject
@dataclass
class AgentWorkbenchRunExecutionService(BaseService):
    """执行一次 Agent Run，并把结果收敛为协议事件和业务状态。"""

    db: SQLAlchemy
    agent_assembly_service: AgentAssemblyService
    run_service: AgentWorkbenchRunService
    redis_repository: AgentWorkbenchRedisRepository
    app_config_service: AppConfigService
    event_queue: AgentWorkbenchEventQueue

    _cancel_signal_poll_interval: ClassVar[float] = 0.25
    _cancel_status_poll_interval: ClassVar[float] = 1.0
    _replay_batch_size: ClassVar[int] = 8
    _replay_batch_interval: ClassVar[float] = 0.05
    _persistence_batch_size: ClassVar[int] = 8
    _persistence_batch_interval: ClassVar[float] = 0.05

    def execute(self, run: WorkbenchRun) -> None:
        """装配并执行 Agent，最终发布 completed、failed 或 cancelled。"""
        started_at = time.monotonic()
        agent: Any | None = None
        projector = AgentProtocolEventProjector(
            run.run_id,
            start_sequence=run.initial_event_sequence,
            root_message_id=f"{run.spec.message_id}:assistant",
        )
        try:
            if self._is_cancelled(run):
                self._finish_cancelled_run(run, projector)
                return
            phase_started_at = time.monotonic()
            self._mark_run_started(run, projector)
            self._log_timing(
                run,
                stage="run.mark_started",
                duration=time.monotonic() - phase_started_at,
            )
            phase_started_at = time.monotonic()
            agent, llm = self._prepare_run_agent(run)
            self._log_timing(
                run,
                stage="agent.prepare",
                duration=time.monotonic() - phase_started_at,
            )
            self._consume_agent_events(run, agent, llm, projector)
        except GraphDrained as exc:
            if exc.reason == "cancel_requested" or self._is_cancelled(run):
                self._finish_cancelled_run(run, projector)
            else:
                self._fail_run(run, projector, exc)
        except Exception as exc:
            self._fail_run(run, projector, exc)
        else:
            if self._is_cancelled(run):
                self._finish_cancelled_run(run, projector)
                return
            self._complete_run(
                run,
                projector,
                latency=max(0.0, time.monotonic() - started_at),
                run_usage=agent.last_run_usage,
            )
        finally:
            # 输入消息转换等流创建前异常不会进入 AgentExecutor.stream_events；
            # 这里幂等关闭已装配的执行器，避免 Backend/Sandbox 泄漏。
            if agent is not None:
                close_agent = getattr(agent, "close", None)
                if callable(close_agent):
                    try:
                        close_agent()
                    except Exception:
                        current_app.logger.exception("关闭 Agent 运行时资源失败")
            self.event_queue.finish(run)
            self._log_timing(
                run,
                stage="run.total",
                duration=time.monotonic() - started_at,
            )

    def hydrate_config(self, run: WorkbenchRun) -> None:
        """在执行边界加载本次 Run 使用的草稿或发布配置。"""
        try:
            app = self.db.session.get(App, run.spec.app_id)
            if app is None:
                raise NotFoundException("应用不存在")
            try:
                app_config = (
                    AgentAppConfigReq.model_validate(
                        self.app_config_service.get_draft_app_config(app)
                    )
                    if run.spec.invoke_from == InvokeFrom.DEBUGGER
                    else self.app_config_service.get_agent_runtime_config(app)
                )
            except (ValidationError, ValidateErrorException) as exc:
                raise AgentInvocationException(f"应用运行配置无效: {exc}") from exc
            run.spec = run.spec.model_copy(
                update={
                    "app_config": app_config,
                    "app_config_hydrated": True,
                    "app_name": app.name,
                    "app_description": app.description,
                    "opening_statement": (
                        app_config.opening_statement
                        if run.include_opening_statement
                        else ""
                    ),
                }
            )
        finally:
            self.db.session.close()

    def _mark_run_started(
        self,
        run: WorkbenchRun,
        projector: AgentProtocolEventProjector,
    ) -> None:
        """将 Run 切换到运行态并发布官方 lifecycle 事件。"""
        run.status = WorkbenchRunStatus.RUNNING
        self.run_service.set_status(run, WorkbenchRunStatus.RUNNING)
        self._publish_event(
            run,
            projector.generated(
                "lifecycle",
                {"event": "running"},
            ),
        )

    def _prepare_run_agent(
        self,
        run: WorkbenchRun,
    ) -> tuple[Any, BaseChatModel]:
        """创建 Run 独占执行器，并在模型运行前归还 ORM 连接。"""
        try:
            if (
                run.spec.invoke_from is not InvokeFrom.ASSISTANT_AGENT
                and not run.spec.app_config_hydrated
            ):
                self.hydrate_config(run)
            return self.agent_assembly_service.build_agent(run.spec)
        finally:
            # Agent 流可能持续数分钟；装配阶段的只读事务不能跟随整个 Run。
            self.db.session.close()

    def _consume_agent_events(
        self,
        run: WorkbenchRun,
        agent: Any,
        llm: BaseChatModel,
        projector: AgentProtocolEventProjector,
    ) -> None:
        """消费 Agent 原始事件，并交给统一事件发布入口。

        每个投影事件先进入本地队列，确保 SSE 能立即收到增量；Redis
        重放和 PostgreSQL 历史事件随后按批刷新。高频 ``messages`` 增量
        不直接更新业务消息表，最终回答由 Run 终态统一写入。
        """
        query = llm.convert_to_human_message(
            run.spec.query,
            run.spec.image_urls,
            dump_agent_attachments(run.spec.attachments),
        )
        if run.spec.input_message_id:
            query.id = run.spec.input_message_id

        raw_stream = agent.stream_events({"messages": [query]})
        drain_requested = False
        stream_started_at = time.monotonic()
        first_raw_event_seen = False
        first_projected_event_seen = False
        projected_event_count = 0
        publish_duration = 0.0
        replay_events: list[WorkbenchEvent] = []
        persistence_events: list[WorkbenchEvent] = []
        last_replay_flush_at = 0.0
        last_persistence_flush_at = 0.0
        try:
            for raw_event in raw_stream:
                if not first_raw_event_seen:
                    first_raw_event_seen = True
                    self._log_timing(
                        run,
                        stage="stream.first_raw_event",
                        duration=time.monotonic() - stream_started_at,
                    )
                if self._is_cancelled(run):
                    if not drain_requested:
                        request_drain = getattr(agent, "request_drain", None)
                        if callable(request_drain):
                            drain_requested = bool(request_drain())
                        if not drain_requested:
                            break
                    continue
                for event in projector.project_many(raw_event):
                    if not first_projected_event_seen:
                        first_projected_event_seen = True
                        self._log_timing(
                            run,
                            stage="stream.first_projected_event",
                            duration=time.monotonic() - stream_started_at,
                        )
                    publish_started_at = time.monotonic()
                    public_event = self._publish_event(
                        run,
                        event,
                        persist_replay=False,
                        persist_database=False,
                    )
                    if public_event is not None:
                        replay_events.append(public_event)
                        now = time.monotonic()
                        should_flush = (
                            public_event.method != "messages"
                            or len(replay_events) >= self._replay_batch_size
                            or now - last_replay_flush_at >= self._replay_batch_interval
                        )
                        if should_flush:
                            self._flush_replay_events(run, replay_events)
                            last_replay_flush_at = now
                        if public_event.method != "messages":
                            persistence_events.append(public_event)
                            should_persist = (
                                len(persistence_events) >= self._persistence_batch_size
                                or now - last_persistence_flush_at
                                >= self._persistence_batch_interval
                            )
                            if should_persist:
                                self._flush_persistence_events(
                                    run,
                                    persistence_events,
                                )
                                last_persistence_flush_at = now
                    publish_duration += time.monotonic() - publish_started_at
                    projected_event_count += 1
        finally:
            flush_started_at = time.monotonic()
            self._flush_replay_events(run, replay_events)
            self._flush_persistence_events(run, persistence_events)
            publish_duration += time.monotonic() - flush_started_at
            close = getattr(raw_stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    current_app.logger.exception("关闭 Agent v3 事件流失败")
            self._log_timing(
                run,
                stage="stream.consume",
                duration=time.monotonic() - stream_started_at,
                event_count=projected_event_count,
                publish_ms=publish_duration * 1000,
            )

    def _publish_event(
        self,
        run: WorkbenchRun,
        raw_event: Mapping[str, Any],
        *,
        persist_replay: bool = True,
        persist_database: bool = True,
    ) -> WorkbenchEvent | None:
        """投影、持久化并发布一条对外可见事件。"""
        event = AgentProtocolEventProjector.to_public_event(raw_event)
        if event is None:
            return None
        if event.method == "values" and isinstance(event.params.data, dict):
            run.state_snapshot = event.params.data
        run.last_event_sequence = event.seq
        # 本地订阅先收到实时事件；Redis 只负责跨 worker 和短期重放，
        # 不应阻塞同 worker 的首包。后续网络写仍保持同步和有序。
        self.event_queue.publish(run, event)
        if persist_replay:
            self.redis_repository.append_event(run.run_id, event)
        if persist_database:
            self.run_service.record_event(run, event)
        return event

    def _flush_replay_events(
        self,
        run: WorkbenchRun,
        events: list[WorkbenchEvent],
    ) -> None:
        """把已实时发布的连续事件批量写入 Redis 重放流。"""
        if not events:
            return
        self.redis_repository.append_events(run.run_id, events)
        events.clear()

    def _flush_persistence_events(
        self,
        run: WorkbenchRun,
        events: list[WorkbenchEvent],
    ) -> None:
        """把连续非消息事件合并为一次 PostgreSQL 事务。

        这里只持久化工具、生命周期和 State 快照；文本增量由 Redis
        重放承载，业务消息正文在 Run 终态写入消息表。
        """
        if not events:
            return
        batch = list(events)
        self.run_service.record_events(run, batch)
        events.clear()

    @staticmethod
    def _log_timing(
        run: WorkbenchRun,
        *,
        stage: str,
        duration: float,
        event_count: int | None = None,
        publish_ms: float | None = None,
    ) -> None:
        """记录 Run 阶段耗时，便于区分装配、框架首事件和发布延迟。"""
        details = ""
        if event_count is not None:
            details += f" event_count={event_count}"
        if publish_ms is not None:
            details += f" publish_ms={publish_ms:.2f}"
        current_app.logger.info(
            "agent_workbench_timing run_id=%s stage=%s duration_ms=%.2f%s",
            run.run_id,
            stage,
            duration * 1000,
            details,
        )

    def _complete_run(
        self,
        run: WorkbenchRun,
        projector: AgentProtocolEventProjector,
        *,
        latency: float,
        run_usage: Mapping[str, Any] | None,
    ) -> None:
        """补齐成功 Run 的消息、快照和终态事件。"""
        for skill_event in projector.finish_pending_skill_events(status="completed"):
            self._publish_event(run, skill_event)
        for pending_message in projector.finish_pending_messages():
            self._publish_event(run, pending_message)
        message_updates = self._build_terminal_message_updates(
            run,
            latency=latency,
            run_usage=run_usage,
        )
        self._publish_event(
            run,
            projector.generated("values", self._compact_state_snapshot(run)),
        )
        run.status = WorkbenchRunStatus.COMPLETED
        self.run_service.finalize_run(
            run,
            WorkbenchRunStatus.COMPLETED,
            message_updates=message_updates,
        )
        self._publish_event(
            run,
            projector.generated(
                "lifecycle",
                {"event": "completed", "status": "completed"},
            ),
        )

    def _fail_run(
        self,
        run: WorkbenchRun,
        projector: AgentProtocolEventProjector,
        exc: Exception,
    ) -> None:
        """收敛失败 Run，保留消息流结束和错误状态。"""
        run.error_message = str(exc)
        for skill_event in projector.finish_pending_skill_events(
            status="failed",
            reason="run_failed",
        ):
            self._publish_event(run, skill_event)
        for pending_message in projector.finish_pending_messages():
            self._publish_event(run, pending_message)
        self._publish_event(
            run,
            projector.generated("values", self._compact_state_snapshot(run)),
        )
        run.status = WorkbenchRunStatus.FAILED
        self.run_service.finalize_run(
            run,
            WorkbenchRunStatus.FAILED,
            error_message=str(exc),
            message_updates={
                "status": MessageStatus.ERROR,
                "error": "Agent 运行失败",
            },
        )
        self._publish_event(
            run,
            projector.generated(
                "lifecycle",
                {
                    "event": "failed",
                    "status": "failed",
                    "error": "Agent 运行失败",
                },
            ),
        )
        if isinstance(exc, _AGENT_PERSISTENCE_ERRORS):
            current_app.logger.error(
                "Agent PostgreSQL 持久化失败 error_type=%s checkpoint_pool_stats=%s",
                type(exc).__name__,
                get_postgres_pool_stats(),
            )
        current_app.logger.exception("Agent 工作台运行失败")

    def _finish_cancelled_run(
        self,
        run: WorkbenchRun,
        projector: AgentProtocolEventProjector,
    ) -> None:
        """发布取消终态，兼容运行中和排队期间取消。"""
        for skill_event in projector.finish_pending_skill_events(
            status="cancelled",
            reason="run_cancelled",
        ):
            self._publish_event(run, skill_event)
        for pending_message in projector.finish_pending_messages():
            self._publish_event(run, pending_message)
        self._publish_event(
            run,
            projector.generated("values", self._compact_state_snapshot(run)),
        )
        run.status = WorkbenchRunStatus.CANCELLED
        self.run_service.finalize_run(
            run,
            WorkbenchRunStatus.CANCELLED,
            message_updates={"status": MessageStatus.STOP},
        )
        self._publish_event(
            run,
            projector.generated(
                "lifecycle",
                {
                    "event": "interrupted",
                    "status": "cancelled",
                    "reason": "cancel_requested",
                },
            ),
        )

    def _is_cancelled(self, run: WorkbenchRun) -> bool:
        """优先读取快速取消信号，并用数据库状态低频兜底。"""
        if run.cancel_requested.is_set():
            return True
        now = time.monotonic()
        if now - run.last_cancel_signal_check >= self._cancel_signal_poll_interval:
            run.last_cancel_signal_check = now
            remote_cancelled = self.redis_repository.is_cancel_requested(run.run_id)
            if remote_cancelled is True:
                return True
        if now - run.last_cancel_status_check < self._cancel_status_poll_interval:
            return False
        run.last_cancel_status_check = now
        return self.run_service.is_cancel_requested(run.run_id)

    @staticmethod
    def _compact_state_snapshot(run: WorkbenchRun) -> dict[str, Any]:
        """终态只保留 UI 需要的轻量 State，消息由业务消息表负责水合。"""
        snapshot = run.state_snapshot
        if not isinstance(snapshot, Mapping):
            return {}
        return {
            key: snapshot[key]
            for key in ("todos", "files", "async_tasks")
            if key in snapshot
        }

    def _build_terminal_message_updates(
        self,
        run: WorkbenchRun,
        *,
        latency: float,
        run_usage: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """从最终 State 构建业务消息更新，不执行额外数据库查询。"""
        state_snapshot = run.state_snapshot or {}
        messages = (
            state_snapshot.get("messages")
            if isinstance(state_snapshot, Mapping)
            else None
        )
        answer = ""
        if isinstance(messages, list):
            for item in reversed(messages):
                if not isinstance(item, Mapping):
                    continue
                if item.get("type") not in {"ai", "assistant"}:
                    continue
                answer = message_content_to_text(item.get("content"))
                if answer:
                    break
        if not answer:
            return {}
        return {
            "answer": answer,
            "status": MessageStatus.NORMAL,
            "latency": latency,
            **ConversationService.build_run_usage_updates(run_usage),
        }


__all__ = ["AgentWorkbenchRunExecutionService"]
