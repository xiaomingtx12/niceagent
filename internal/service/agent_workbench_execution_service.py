"""工作台 Run 的 Agent 执行和终态收敛。"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar

from injector import inject
from langchain_core.messages import BaseMessage
from langgraph.errors import GraphDrained
from psycopg_pool import PoolTimeout

from internal.core.agent_v2.assembly.observability.run_observer import (
    PlatformAgentRunObserver,
)
from internal.core.agent_v2.execution.agent_executor import AgentExecutor
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
from internal.entity.conversation_entity import MessageStatus
from internal.exception import CustomException
from internal.extension.logging_extension import logger
from internal.repository import AgentWorkbenchRedisRepository
from internal.schema.agent_workbench_schema import WorkbenchEvent
from pkg.response import HttpCode
from pkg.sqlalchemy import SQLAlchemy

from .agent_assembly_service import AgentAssemblyService
from .agent_workbench_event_queue import AgentWorkbenchEventQueueManager
from .agent_workbench_run_service import AgentWorkbenchRunService
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
    queue_manager: AgentWorkbenchEventQueueManager

    _cancel_signal_poll_interval: ClassVar[float] = 0.25
    _cancel_status_poll_interval: ClassVar[float] = 1.0
    _replay_batch_size: ClassVar[int] = 8
    _replay_batch_interval: ClassVar[float] = 0.05
    _persistence_batch_size: ClassVar[int] = 8
    _persistence_batch_interval: ClassVar[float] = 0.05

    # -------------------- Core execution flow --------------------

    def execute(self, run: WorkbenchRun) -> None:
        """装配并执行 Agent，最终发布 completed、failed 或 cancelled。"""
        # 1.创建本次 Run 的观测器和协议事件投影器。
        started_at = time.monotonic()
        agent: AgentExecutor | None = None
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
        try:
            # 2.处理执行前取消，并尝试把 Run 切换到 running 状态。
            if self._is_cancelled(run):
                self._finish_cancelled_run(
                    run,
                    projector,
                    observer=observer,
                    started_at=started_at,
                )
                return
            phase_started_at = time.monotonic()
            started = self._mark_run_started(run, projector)
            self._log_timing(
                run,
                stage="run.mark_started",
                duration=time.monotonic() - phase_started_at,
                observer=observer,
            )
            if not started:
                # 启动与取消并发时，Run 仍处于 cancelling，直接发布取消终态。
                self._finish_cancelled_run(
                    run,
                    projector,
                    observer=observer,
                    started_at=started_at,
                )
                return

            # 3.装配当前 Run 独占的 Agent 执行器和输入消息。
            phase_started_at = time.monotonic()
            agent, input_message = self._prepare_run_agent(run, observer)
            self._log_timing(
                run,
                stage="agent.prepare",
                duration=time.monotonic() - phase_started_at,
                observer=observer,
            )

            # 4.消费图事件，实时投影并批量持久化平台事件。
            self._consume_agent_events(
                run,
                agent,
                input_message,
                projector,
                observer,
            )
        except GraphDrained as exc:
            # 5.把协作式停止区分为取消和真实失败。
            if exc.reason == "cancel_requested" or self._is_cancelled(run):
                self._finish_cancelled_run(
                    run,
                    projector,
                    observer=observer,
                    started_at=started_at,
                )
            else:
                self._fail_run(
                    run,
                    projector,
                    exc,
                    observer=observer,
                    started_at=started_at,
                )
        except Exception as exc:
            # 6.统一收敛执行异常和失败观测。
            self._fail_run(
                run,
                projector,
                exc,
                observer=observer,
                started_at=started_at,
            )
        else:
            # 7.正常结束前再次确认是否收到取消请求。
            if self._is_cancelled(run):
                self._finish_cancelled_run(
                    run,
                    projector,
                    observer=observer,
                    started_at=started_at,
                )
                return
            self._complete_run(
                run,
                projector,
                latency=max(0.0, time.monotonic() - started_at),
                observer=observer,
                started_at=started_at,
            )
        finally:
            # 8.无论结果如何都幂等关闭执行器并结束本地事件队列。
            if agent is not None:
                try:
                    agent.close()
                except Exception:
                    logger.exception("关闭 Agent 运行时资源失败")
            self.queue_manager.finish(run)

    def _mark_run_started(
        self,
        run: WorkbenchRun,
        projector: AgentProtocolEventProjector,
    ) -> bool:
        """将 Run 切换到运行态并发布官方 lifecycle 事件。"""
        # 1.持久化并更新内存中的 Run 状态。
        run.status = WorkbenchRunStatus.RUNNING
        self.run_service.set_status(run, WorkbenchRunStatus.RUNNING)
        if self.run_service.is_cancel_requested(run.run_id):
            run.status = WorkbenchRunStatus.CANCELLING
            return False

        # 2.发布客户端可见的 running 生命周期事件。
        self._publish_event(
            run,
            projector.generated(
                "lifecycle",
                {"event": "running"},
            ),
        )
        return True

    def _prepare_run_agent(
        self,
        run: WorkbenchRun,
        observer: PlatformAgentRunObserver,
    ) -> tuple[AgentExecutor, BaseMessage]:
        """创建 Run 独占执行器，并在模型运行前归还 ORM 连接。"""
        # 1.调用装配服务创建当前 Run 的执行器和输入消息。
        try:
            return self.agent_assembly_service.build_agent(
                run.spec,
                observer=observer,
            )
        finally:
            # 2.Agent 流可能持续数分钟，装配阶段 Session 不跟随整个 Run。
            self.db.session.close()

    def _consume_agent_events(
        self,
        run: WorkbenchRun,
        agent: AgentExecutor,
        input_message: BaseMessage,
        projector: AgentProtocolEventProjector,
        observer: PlatformAgentRunObserver,
    ) -> None:
        """消费 Agent 原始事件，并交给统一事件发布入口。

        每个投影事件先进入本地队列，确保 SSE 能立即收到增量；Redis
        重放和 PostgreSQL 历史事件随后按批刷新。高频 ``messages`` 增量
        不直接更新业务消息表，最终回答由 Run 终态统一写入。
        """
        # 1.创建原始事件流和批量缓冲区。
        raw_stream = agent.stream_events({"messages": [input_message]})
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
        stream_failed = False
        try:
            # 2.逐个消费图事件，处理取消并投影为平台协议事件。
            for raw_event in raw_stream:
                if not first_raw_event_seen:
                    first_raw_event_seen = True
                    self._log_timing(
                        run,
                        stage="stream.first_raw_event",
                        duration=time.monotonic() - stream_started_at,
                        observer=observer,
                    )
                if self._is_cancelled(run):
                    if not drain_requested:
                        drain_requested = agent.request_drain()
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
                            observer=observer,
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
        except Exception:
            # finally 中的批量刷新不能覆盖 Agent 流本身的原始异常。
            stream_failed = True
            raise
        finally:
            # 3.流结束时刷新剩余批次、关闭流并记录消费指标。
            flush_started_at = time.monotonic()
            flush_error: Exception | None = None
            try:
                self._flush_replay_events(run, replay_events)
                self._flush_persistence_events(run, persistence_events)
            except Exception as exc:
                if stream_failed:
                    logger.exception("刷新 Agent 事件批次失败，保留原始流异常")
                else:
                    flush_error = exc
            publish_duration += time.monotonic() - flush_started_at
            close = getattr(raw_stream, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.exception("关闭 Agent v3 事件流失败")
            self._log_timing(
                run,
                stage="stream.consume",
                duration=time.monotonic() - stream_started_at,
                event_count=projected_event_count,
                publish_ms=publish_duration * 1000,
                observer=observer,
            )
            if flush_error is not None:
                raise flush_error

    # -------------------- Run finalization flow --------------------

    def _complete_run(
        self,
        run: WorkbenchRun,
        projector: AgentProtocolEventProjector,
        *,
        latency: float,
        observer: PlatformAgentRunObserver,
        started_at: float,
    ) -> None:
        """补齐成功 Run 的消息、快照和终态事件。"""
        # 1.结束尚未完成的 Skill 和消息事件，并发布最终 State 快照。
        self._finish_projected_events(
            run,
            projector,
            skill_status="completed",
        )

        # 2.收敛成功状态、观测数据和业务消息。
        self._finalize_terminal_run(
            run,
            observer,
            status=WorkbenchRunStatus.COMPLETED,
            started_at=started_at,
            message_updates=lambda usage: self._build_terminal_message_updates(
                run,
                latency=latency,
                run_usage=usage,
            ),
        )

        # 3.发布 completed 生命周期事件。
        self._publish_terminal_event(
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
        *,
        observer: PlatformAgentRunObserver,
        started_at: float,
    ) -> None:
        """收敛失败 Run，保留消息流结束和错误状态。"""
        # 1.提取对外错误信息。
        run.error_message = str(exc)
        if isinstance(exc, CustomException):
            error_code = exc.code.value
            public_error = exc.message or "Agent 运行失败"
        else:
            error_code = HttpCode.FAIL.value
            public_error = "Agent 运行失败"

        # 2.结束未完成事件并发布失败时的最终 State 快照。
        self._finish_projected_events(
            run,
            projector,
            skill_status="failed",
            skill_reason="run_failed",
        )

        # 3.持久化失败状态、消息错误和观测数据。
        self._finalize_terminal_run(
            run,
            WorkbenchRunStatus.FAILED,
            observer=observer,
            started_at=started_at,
            error_message=str(exc),
            error=exc,
            message_updates=lambda usage: {
                "status": MessageStatus.ERROR,
                "error": public_error,
                **ConversationService.build_run_usage_updates(usage),
            },
        )

        # 4.发布 failed 生命周期事件并记录持久化异常上下文。
        self._publish_terminal_event(
            run,
            projector.generated(
                "lifecycle",
                {
                    "event": "failed",
                    "status": "failed",
                    "code": error_code,
                    "error": public_error,
                },
            ),
        )
        if isinstance(exc, _AGENT_PERSISTENCE_ERRORS):
            logger.error(
                "Agent PostgreSQL 持久化失败 error_type=%s checkpoint_pool_stats=%s",
                type(exc).__name__,
                get_postgres_pool_stats(),
            )
        logger.exception("Agent 工作台运行失败")

    def _finish_cancelled_run(
        self,
        run: WorkbenchRun,
        projector: AgentProtocolEventProjector,
        *,
        observer: PlatformAgentRunObserver,
        started_at: float,
    ) -> None:
        """发布取消终态，兼容运行中和排队期间取消。"""
        # 1.结束尚未完成的 Skill 和消息事件，并发布取消时的 State 快照。
        self._finish_projected_events(
            run,
            projector,
            skill_status="cancelled",
            skill_reason="run_cancelled",
        )

        # 2.持久化取消状态、消息状态和观测数据。
        self._finalize_terminal_run(
            run,
            WorkbenchRunStatus.CANCELLED,
            observer=observer,
            started_at=started_at,
            message_updates=lambda usage: {
                "status": MessageStatus.STOP,
                **ConversationService.build_run_usage_updates(usage),
            },
        )

        # 3.发布 interrupted 生命周期事件。
        self._publish_terminal_event(
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

    def _finish_projected_events(
        self,
        run: WorkbenchRun,
        projector: AgentProtocolEventProjector,
        *,
        skill_status: str,
        skill_reason: str | None = None,
    ) -> None:
        """统一关闭挂起的 Skill/消息事件并发布终态 State 快照。"""
        # 1.关闭仍在执行的 Skill 文件读取事件。
        for skill_event in projector.finish_pending_skill_events(
            status=skill_status,
            reason=skill_reason,
        ):
            self._publish_event(run, skill_event)

        # 2.补齐尚未收到 provider finish 标记的文本消息事件。
        for pending_message in projector.finish_pending_messages():
            self._publish_event(run, pending_message)

        # 3.发布终态所需的轻量 State 快照。
        self._publish_event(
            run,
            projector.generated("values", self._compact_state_snapshot(run)),
        )

    def _finalize_terminal_run(
        self,
        run: WorkbenchRun,
        *,
        observer: PlatformAgentRunObserver,
        status: WorkbenchRunStatus,
        started_at: float,
        message_updates: Callable[
            [Mapping[str, Any]], Mapping[str, Any]
        ],
        error_message: str = "",
        error: BaseException | None = None,
    ) -> None:
        """统一收敛 Run 状态、观测快照和业务消息。"""
        # 1.更新内存中的终态，并结束平台观测 Trace。
        run.status = status
        self._finish_observability(
            run,
            observer,
            status=status,
            started_at=started_at,
            error=error,
        )

        # 2.基于最终观测结果生成业务消息更新。
        run_usage = observer.build_run_usage()
        updates = dict(message_updates(run_usage))

        # 3.在一个事务中持久化 AgentRun、业务消息和观测快照。
        self.run_service.finalize_run(
            run,
            status,
            error_message=error_message,
            message_updates=updates,
            observability=observer.snapshot(),
        )

    # -------------------- Internal event helpers --------------------

    def _publish_event(
        self,
        run: WorkbenchRun,
        raw_event: Mapping[str, Any],
        *,
        persist_replay: bool = True,
        persist_database: bool = True,
    ) -> WorkbenchEvent | None:
        """投影、持久化并发布一条对外可见事件。"""
        # 1.把框架原始事件投影为平台公开事件；不可见事件直接忽略。
        event = AgentProtocolEventProjector.to_public_event(raw_event)
        if event is None:
            return None

        # 2.更新 Run 的 State 快照和事件序号。
        if event.method == "values" and isinstance(event.params.data, dict):
            run.state_snapshot = event.params.data
        run.last_event_sequence = event.seq

        # 3.先发布本地实时事件，再按需写入 Redis 和 PostgreSQL。
        # 本地订阅先收到实时事件；Redis 只负责跨 worker 和短期重放，
        # 不应阻塞同 worker 的首包。后续网络写仍保持同步和有序。
        self.queue_manager.publish(run, event)
        if persist_replay:
            replayed = self.redis_repository.append_event(run.run_id, event)
            if not replayed:
                logger.warning(
                    "Agent 工作台事件 Redis 重放写入失败 run_id=%s seq=%s",
                    run.run_id,
                    event.seq,
                )
        if persist_database:
            self.run_service.record_event(run, event)
        return event

    def _publish_terminal_event(
        self,
        run: WorkbenchRun,
        raw_event: Mapping[str, Any],
    ) -> None:
        """发布终态生命周期事件；持久化失败不能逆转已落库的终态。"""
        try:
            self._publish_event(run, raw_event)
        except Exception:
            logger.exception(
                "Agent 工作台终态事件发布失败 run_id=%s，保留已持久化终态",
                run.run_id,
            )

    def _flush_replay_events(
        self,
        run: WorkbenchRun,
        events: list[WorkbenchEvent],
    ) -> None:
        """把已实时发布的连续事件批量写入 Redis 重放流。"""
        # 1.没有待刷新的事件时直接返回。
        if not events:
            return

        # 2.批量追加到 Redis；Redis 是重放通道，故障时不阻塞主 Run。
        persisted = self.redis_repository.append_events(run.run_id, events)
        if not persisted:
            logger.warning(
                "Agent 工作台事件 Redis 重放写入失败 run_id=%s event_count=%s",
                run.run_id,
                len(events),
            )

        # 3.无论成功与否都清空批次，避免 Redis 故障时内存无限增长。
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
        # 1.没有待持久化事件时直接返回。
        if not events:
            return

        # 2.复制当前批次，交给 Run Service 在一个事务中保存。
        batch = list(events)
        self.run_service.record_events(run, batch)

        # 3.清空已成功持久化的缓冲区。
        events.clear()

    @staticmethod
    def _log_timing(
        run: WorkbenchRun,
        *,
        stage: str,
        duration: float,
        observer: PlatformAgentRunObserver,
        event_count: int | None = None,
        publish_ms: float | None = None,
    ) -> None:
        """记录 Run 阶段耗时，便于区分装配、框架首事件和发布延迟。"""
        # 1.整理可选的事件数量和发布耗时字段。
        details = ""
        if event_count is not None:
            details += f" event_count={event_count}"
        if publish_ms is not None:
            details += f" publish_ms={publish_ms:.2f}"

        # 2.写入平台观测快照。
        observer.record_stage(
            stage,
            duration * 1000,
            event_count=event_count,
            publish_ms=publish_ms,
        )

        # 3.输出可按 Run 检索的结构化日志。
        logger.info(
            "agent_workbench_timing run_id=%s stage=%s duration_ms=%.2f%s",
            run.run_id,
            stage,
            duration * 1000,
            details,
        )

    def _is_cancelled(self, run: WorkbenchRun) -> bool:
        """优先读取快速取消信号，并用数据库状态低频兜底。"""
        # 1.优先读取当前进程内的即时取消信号。
        if run.cancel_requested.is_set():
            return True

        # 2.按间隔读取 Redis，感知其他进程发出的取消请求。
        now = time.monotonic()
        if now - run.last_cancel_signal_check >= self._cancel_signal_poll_interval:
            run.last_cancel_signal_check = now
            remote_cancelled = self.redis_repository.is_cancel_requested(run.run_id)
            if remote_cancelled is True:
                return True

        # 3.按更低频率读取数据库，作为跨进程最终兜底。
        if now - run.last_cancel_status_check < self._cancel_status_poll_interval:
            return False
        run.last_cancel_status_check = now
        return self.run_service.is_cancel_requested(run.run_id)

    @staticmethod
    def _compact_state_snapshot(run: WorkbenchRun) -> dict[str, Any]:
        """终态只保留 UI 需要的轻量 State，消息由业务消息表负责水合。"""
        # 1.确认当前 State 是可读取的映射对象。
        snapshot = run.state_snapshot
        if not isinstance(snapshot, Mapping):
            return {}

        # 2.只保留工作台终态所需的轻量字段。
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
        # 1.从最终 State 中反向查找最后一条有效的 Assistant 消息。
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

        # 2.构造消息状态、延迟和用量更新。
        updates = {
            "status": MessageStatus.NORMAL,
            "latency": latency,
            **ConversationService.build_run_usage_updates(run_usage),
        }
        if answer:
            updates["answer"] = answer

        # 3.返回给 Run Service 的业务消息更新。
        return updates

    def _finish_observability(
        self,
        run: WorkbenchRun,
        observer: PlatformAgentRunObserver,
        *,
        status: WorkbenchRunStatus,
        started_at: float,
        error: BaseException | None = None,
    ) -> None:
        """在数据库终态前收敛平台 Trace，确保快照包含总耗时。"""
        # 1.计算当前 Run 的总耗时。
        duration = max(0.0, time.monotonic() - started_at)

        # 2.记录总耗时阶段，确保观测快照包含完整链路。
        self._log_timing(
            run,
            stage="run.total",
            duration=duration,
            observer=observer,
        )

        # 3.结束平台 Trace，并关联最终状态或异常。
        observer.finish(status.value, error=error)


__all__ = ["AgentWorkbenchRunExecutionService"]
