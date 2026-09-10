"""摘要生成的公开进度通知；不参与调用链和用量统计。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from time import monotonic
from typing import Any
from uuid import UUID

from langgraph.config import get_stream_writer

from internal.extension.logging_extension import logger


@dataclass
class _SummaryCall:
    """在开始回调中捕获子图 writer，结束回调可能已经离开该图上下文。"""

    writer: Callable[[Any], None]
    started_at: datetime
    started_monotonic: float


class SummaryProgressPublisher:
    """只发布 SDK 标记的摘要生成进度，不推断上下文是否已完成压缩。"""

    def __init__(self) -> None:
        """每次 Run 独占状态，以调用 ID 隔离并发子图。"""
        self._calls: dict[str, _SummaryCall] = {}
        self._lock = RLock()

    def start(self, run_id: UUID, metadata: Mapping[str, Any] | None) -> None:
        """普通模型调用和无图上下文的调用都不产生公开通知。"""
        if not metadata or metadata.get("lc_source") != "summarization":
            return
        try:
            writer = get_stream_writer()
        except RuntimeError:
            return
        call_id = str(run_id)
        with self._lock:
            if call_id in self._calls:
                return
            call = _SummaryCall(writer, datetime.now(UTC), monotonic())
            self._calls[call_id] = call
            self._publish(call_id, call, "summarizing")

    def finish(
        self,
        run_id: UUID,
        phase: str,
        *,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """消费开始时的通知状态，重复或迟到的结束回调不会再次发布。"""
        call_id = str(run_id)
        with self._lock:
            call = self._calls.pop(call_id, None)
            if call is not None:
                self._publish(call_id, call, phase, usage=usage)

    def clear(self) -> None:
        """Run 结束后释放图 writer，避免继续持有已关闭的流。"""
        with self._lock:
            self._calls.clear()

    @staticmethod
    def _publish(
        call_id: str,
        call: _SummaryCall,
        phase: str,
        *,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """公开载荷不含正文或错误详情；通知失败不能影响模型统计。"""
        payload = {
            "type": "context_compaction",
            "scope": "summary_generation",
            "compaction_id": call_id,
            "phase": phase,
            "started_at": int(call.started_at.timestamp() * 1000),
            "ended_at": (
                int(datetime.now(UTC).timestamp() * 1000)
                if phase != "summarizing"
                else None
            ),
            "duration_ms": max(0, int((monotonic() - call.started_monotonic) * 1000)),
        }
        if usage is not None:
            payload.update(
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
            )
        try:
            call.writer(payload)
        except Exception:
            logger.exception("摘要观测事件发布失败 call_id=%s", call_id)
