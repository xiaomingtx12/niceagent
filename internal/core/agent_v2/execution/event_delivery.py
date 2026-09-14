"""图事件的输出审核和交付，不参与图执行及 Backend 生命周期。"""

from collections.abc import Callable, Mapping
from threading import Event
from typing import Any

from internal.core.agent_v2.entity.agent_event import AgentEvent
from internal.core.agent_v2.execution.message_parser import current_turn_ai_contents
from internal.core.agent_v2.execution.event_normalizer import AgentEventNormalizer
from internal.core.agent_v2.middleware.review_middleware import ReviewMiddleware


class AgentEventDelivery:
    """先审核，再在 Core 出口规范化事件，业务订阅者无需处理框架对象。"""

    def __init__(self, review: ReviewMiddleware, on_event: Callable[[AgentEvent], None] | None) -> None:
        """缓冲区只属于一次 execute；没有订阅者时仍审核输出，但不缓存事件。"""
        self._review = review
        self._on_event = on_event
        self._pending: list[Mapping[str, Any]] = []
        self._normalizer = AgentEventNormalizer()

    def accept(self, raw: Mapping[str, Any]) -> None:
        """values 包括子图状态；消息增量也必须等整轮审核结束才能交付。"""
        # 1.未启用输出审核时直接进入 Core 规范化出口。
        if not self._review.output_enabled:
            self._publish(raw)
            return
        data = raw.get("params", {}).get("data")
        # 2.审核状态快照中的当前轮回答，命中后整轮事件都不再公开。
        if raw.get("method") == "values" and isinstance(data, Mapping):
            for content in current_turn_ai_contents(data.get("messages", []) or []):
                self._review.check_output(content)
        # 3.启用审核时暂存原始事件，审核通过后按原顺序回放。
        if self._on_event is not None:
            self._pending.append(raw)

    def flush(self, cancel_requested: Event) -> None:
        """只在图成功结束后回放，取消或回调异常时释放尚未交付的事件。"""
        try:
            # 1.执行成功且未取消时才回放整轮事件；期间到达取消立即停止。
            if self._on_event is None:
                return
            for raw in self._pending:
                if cancel_requested.is_set():
                    break
                self._publish(raw)
        finally:
            self.discard()

    def discard(self) -> None:
        """失败或取消后立即释放原生载荷，避免异常堆栈持有整轮缓存。"""
        self._pending.clear()

    def finish(self) -> None:
        """只收尾已经交付的文本；未通过审核的缓存从未进入规范化器。"""
        # Core 负责补齐尚未收到结束分片的消息。
        if self._on_event is not None:
            for event in self._normalizer.finish():
                self._on_event(event)

    def _publish(self, raw: Mapping[str, Any]) -> None:
        """一次框架事件可拆成多个标准分片，按顺序直接交给订阅者。"""
        if self._on_event is not None:
            for event in self._normalizer.accept(raw):
                self._on_event(event)
