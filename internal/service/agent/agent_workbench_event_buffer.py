"""一次工作台执行的事件缓冲，不跨 Run 共享可变状态。"""

import time
from collections.abc import Mapping

from internal.core.agent_v2.entity.agent_event import AgentEvent
from internal.core.agent_v2.observability.run_observer import PlatformAgentRunObserver
from internal.entity import WorkbenchRun
from internal.extension.logging_extension import logger
from internal.schema.agent.agent_workbench_schema import WorkbenchEvent
from internal.service.agent_protocol.event_projector import AgentProtocolEventProjector

from .agent_workbench_event_queue import AgentWorkbenchEventQueueManager
from .agent_workbench_run_service import AgentWorkbenchRunService


class AgentWorkbenchEventBuffer:
    """集中管理流式批次及首字计时，所有通知都发生在数据库提交之后。"""

    def __init__(
        self,
        run: WorkbenchRun,
        projector: AgentProtocolEventProjector,
        observer: PlatformAgentRunObserver,
        run_service: AgentWorkbenchRunService,
        queue_manager: AgentWorkbenchEventQueueManager,
        *,
        started_at: float,
        batch_size: int,
    ) -> None:
        """缓冲只由本次执行的事件回调使用，不拥有 Agent 或数据库 Session。"""
        self._run = run
        self._projector = projector
        self._observer = observer
        self._run_service = run_service
        self._queue_manager = queue_manager
        self._started_at = started_at
        self._batch_size = batch_size
        self._pending: list[WorkbenchEvent] = []
        self._last_flush_at = time.monotonic()
        self._first_text_published = False

    def receive(self, agent_event: AgentEvent) -> None:
        """按事件到达顺序缓冲，首字和阶段边界立即提交，其余文本合并提交。"""
        for projected in self._projector.project_many(agent_event):
            # 1.跳过非公开事件，仅将客户端可重放的事件加入待提交批次。
            event = AgentProtocolEventProjector.to_public_event(projected)
            if event is None:
                continue
            self._pending.append(event)
            data = event.params.data
            event_name = data.get("event") if isinstance(data, Mapping) else None

            # 2.首字计时只认文本增量，推理、工具和空文本不计入。
            first_text = False
            delta = data.get("delta") if isinstance(data, Mapping) else None
            is_text_message = event.method == "messages" and event_name == "content-block-delta"
            is_text_delta = isinstance(delta, Mapping) and delta.get("type") == "text-delta"
            if is_text_message and is_text_delta:
                first_text = not self._first_text_published and bool(delta.get("text"))

            # 3.逐项判断刷新原因；只有普通文本且未达到阈值时继续积累。
            flush_now = False
            if first_text:
                flush_now = True  # 首个文本立即交付，避免批量等待增加首字延迟。
            elif event.method != "messages":
                flush_now = True  # 工具、状态等事件立即交付，先刷出它们之前的文本。
            elif event_name in {"content-block-finish", "message-finish"}:
                flush_now = True  # 消息阶段已结束，不能把尾批留到下一阶段。
            elif len(self._pending) >= self._batch_size:
                flush_now = True  # 已达到单批数量上限。
            elif time.monotonic() - self._last_flush_at >= 0.1:
                flush_now = True  # 有新事件到达时检查时间阈值，不另建定时线程。
            if not flush_now:
                continue

            # 4.先落库再通知；只有首字成功交付后才记录首字耗时。
            self._flush()
            if not first_text:
                continue
            self._first_text_published = True
            duration_ms = (self._last_flush_at - self._started_at) * 1000
            self._observer.record_stage("delivery.first_text", duration_ms)
            logger.info(
                "agent_delivery_timing run_id=%s stage=first_text duration_ms=%.2f",
                self._run.run_id, duration_ms,
            )

    def finish(self) -> None:
        """补齐消息结束分片后刷新尾批；失败时保留批次供上层处理原始错误。"""
        for raw in self._projector.finish_pending_messages():
            event = AgentProtocolEventProjector.to_public_event(raw)
            if event is not None:
                self._pending.append(event)
        self._flush()

    def _flush(self) -> None:
        """提交失败不推进游标；通知失败也不丢弃尚未完成交付的批次。"""
        if not self._pending:
            return
        self._run_service.record_events(self._run, self._pending)
        self._run.last_event_sequence = self._pending[-1].seq
        self._queue_manager.notify_events(self._run.conversation_id, self._run.spec.invoke_from)
        self._pending.clear()
        self._last_flush_at = time.monotonic()
