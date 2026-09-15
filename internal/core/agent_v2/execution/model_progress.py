"""模型请求与摘要生成的公开进度通知；不参与调用链和用量统计。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from time import monotonic
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from langgraph.config import get_stream_writer

from internal.core.agent_v2.observability.callback_metadata import exception_status
from internal.core.agent_v2.observability.token_usage import normalize_llm_result
from internal.extension.logging_extension import logger


@dataclass
class _ModelCall:
    """在开始回调中捕获子图 writer，结束回调可能已经离开该图上下文。"""

    writer: Callable[[Any], None]
    started_at: datetime
    started_monotonic: float
    summarization: bool
    publish_lock: RLock = field(default_factory=RLock)


class ModelProgressCallback(BaseCallbackHandler):
    """请求开始即通知工作台；摘要调用仍使用独立的压缩进度语义。"""

    run_inline = True
    raise_error = False

    def __init__(self) -> None:
        """每次执行独占状态，以调用 ID 隔离并发子图。"""
        super().__init__()
        self._calls: dict[str, _ModelCall] = {}
        self._lock = RLock()
        self._closed = False

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: list[list[Any]], *, run_id: UUID,
        metadata: dict[str, Any] | None = None, **kwargs: Any,
    ) -> None:
        """独立订阅模型开始，观测统计开关不影响用户可见进度。"""
        self._start(run_id, metadata)

    def on_llm_start(
        self, serialized: dict[str, Any], prompts: list[str], *, run_id: UUID,
        metadata: dict[str, Any] | None = None, **kwargs: Any,
    ) -> None:
        """非聊天模型沿用同一调用生命周期。"""
        self._start(run_id, metadata)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        """普通调用只发布终态，摘要保留原有的用量展示。"""
        with self._lock:
            call = self._calls.pop(str(run_id), None)
        if call is not None:
            usage = normalize_llm_result(response) if call.summarization else None
            public_usage = usage.to_dict() if usage and usage.present else None
            with call.publish_lock:
                self._publish(str(run_id), call, "completed", usage=public_usage)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """消费调用状态后发布终态，重复结束不会再次通知。"""
        phase = "cancelled" if exception_status(error) == "cancelled" else "failed"
        with self._lock:
            call = self._calls.pop(str(run_id), None)
        if call is not None:
            with call.publish_lock:
                self._publish(str(run_id), call, phase)

    def close(self) -> None:
        """执行器关闭时释放子图 writer，并拒绝迟到的开始回调。"""
        with self._lock:
            self._closed = True
            self._calls.clear()

    def _start(self, run_id: UUID, metadata: Mapping[str, Any] | None) -> None:
        """在首个 Token 之前捕获 writer；无图上下文的调用不公开。"""
        # 1.在模型所属图内捕获 writer，结束回调不再依赖当时的上下文。
        try:
            writer = get_stream_writer()
        except RuntimeError:
            return
        call_id = str(run_id)
        with self._lock:
            # 2.登记调用并占用发布锁，保证并发结束回调不会先于开始通知。
            if self._closed or call_id in self._calls:
                return
            call = _ModelCall(
                writer=writer,
                started_at=datetime.now(UTC),
                started_monotonic=monotonic(),
                summarization=bool(metadata and metadata.get("lc_source") == "summarization"),
            )
            call.publish_lock.acquire()
            self._calls[call_id] = call
        try:
            # 3.在注册表锁外发布；不同模型调用不相互等待。
            self._publish(call_id, call, "summarizing" if call.summarization else "started")
        finally:
            call.publish_lock.release()

    @staticmethod
    def _publish(
        call_id: str,
        call: _ModelCall,
        phase: str,
        *,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """公开载荷不含正文或错误详情；通知失败不能影响模型统计。"""
        payload = {
            "phase": phase,
            "started_at": int(call.started_at.timestamp() * 1000),
            "ended_at": (
                int(datetime.now(UTC).timestamp() * 1000)
                if phase not in {"summarizing", "started"}
                else None
            ),
            "duration_ms": max(0, int((monotonic() - call.started_monotonic) * 1000)),
        }
        if call.summarization:
            payload.update(type="context_compaction", scope="summary_generation", compaction_id=call_id)
        else:
            payload.update(type="model_progress", model_run_id=call_id)
        if usage is not None:
            payload.update(
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
            )
        try:
            call.writer(payload)
        except Exception:
            logger.exception("模型进度事件发布失败 call_id=%s", call_id)
