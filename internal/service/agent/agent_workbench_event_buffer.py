"""一次工作台执行的事件缓冲，不跨 Run 共享可变状态。"""

import time
from collections.abc import Mapping

from internal.core.agent_v2 import (
    AgentEvent,
    PlatformAgentRunObserver,
)
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
        self._first_reasoning_published = False

    def receive(self, agent_event: AgentEvent) -> None:
        """按事件到达顺序缓冲，首段推理、首字和阶段边界立即提交。"""
        for event in self._projector.project_many(agent_event):
            # 1.投影器只返回已校验、可重放的公开事件。
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
            first_reasoning = (
                is_text_message and isinstance(delta, Mapping)
                and delta.get("type") == "reasoning-delta" and bool(delta.get("reasoning"))
                and not self._first_reasoning_published
            )

            # 3.逐项判断刷新原因；后续正文与推理增量按批量阈值提交。
            flush_now = False
            if first_text or first_reasoning:
                flush_now = True  # 两种首片段分别立即交付，推理不消耗正文的首字标记。
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
            if first_reasoning:
                self._first_reasoning_published = True
                duration_ms = (self._last_flush_at - self._started_at) * 1000
                self._observer.record_stage("delivery.first_reasoning", duration_ms)
                logger.info(
                    "agent_delivery_timing run_id=%s stage=first_reasoning duration_ms=%.2f",
                    self._run.run_id, duration_ms,
                )
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
        self._pending.extend(self._projector.finish_pending_messages())
        self._flush()

    def _flush(self) -> None:
        """提交失败不推进游标；通知失败也不丢弃尚未完成交付的批次。"""
        if not self._pending:
            return
        self._run_service.record_events(self._run, self._coalesce_deltas(self._pending))
        self._run.last_event_sequence = self._pending[-1].seq
        self._queue_manager.notify_events(self._run.conversation_id, self._run.spec.invoke_from)
        self._pending.clear()
        self._last_flush_at = time.monotonic()

    @staticmethod
    def _coalesce_deltas(events: list[WorkbenchEvent]) -> list[WorkbenchEvent]:
        """只合并尚未提交的相邻同块增量；沿用最后一个序号，重连游标允许跳号。"""
        merged: list[WorkbenchEvent] = []
        for event in events:
            data = event.params.data
            delta = data.get("delta") if isinstance(data, Mapping) else None
            field = {"text-delta": "text", "reasoning-delta": "reasoning"}.get(
                delta.get("type") if isinstance(delta, Mapping) else None
            )
            if (
                merged and event.method == "messages" and field
                and data.get("event") == "content-block-delta"
                and set(delta) == {"type", field} and isinstance(delta[field], str)
            ):
                previous = merged[-1]
                previous_data = previous.params.data
                previous_delta = previous_data.get("delta") if isinstance(previous_data, Mapping) else None
                if (
                    previous.method == event.method
                    and previous.params.namespace == event.params.namespace
                    and previous.params.node == event.params.node
                    and isinstance(previous_delta, Mapping)
                    and set(previous_delta) == {"type", field}
                    and isinstance(previous_delta[field], str)
                    and previous_delta["type"] == delta["type"]
                    and {key: value for key, value in previous_data.items() if key != "delta"}
                    == {key: value for key, value in data.items() if key != "delta"}
                ):
                    combined = {**data, "delta": {**delta, field: previous_delta[field] + delta[field]}}
                    merged[-1] = event.model_copy(update={
                        "params": event.params.model_copy(update={"data": combined}),
                    })
                    continue
            merged.append(event)
        return merged
