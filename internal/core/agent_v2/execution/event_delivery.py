"""图事件的输出审核和交付，不参与图执行及 Backend 生命周期。"""

from collections.abc import Callable, Mapping
from threading import Event

from internal.core.agent_v2.entity.agent_event import AgentEvent
from internal.core.agent_v2.execution.message_parser import current_turn_ai_contents
from internal.core.agent_v2.middleware.review_middleware import ReviewMiddleware


class AgentEventDelivery:
    """开启审核时整轮通过才公开事件，保留 SDK 载荷的原生类型。"""

    def __init__(self, review: ReviewMiddleware, on_event: Callable[[AgentEvent], None] | None) -> None:
        """缓冲区只属于一次 execute；没有订阅者时仍审核输出，但不缓存事件。"""
        self._review = review
        self._on_event = on_event
        self._pending: list[AgentEvent] = []

    def accept(self, event: AgentEvent) -> None:
        """values 包括子图状态；消息增量也必须等整轮审核结束才能交付。"""
        if not self._review.output_enabled:
            if self._on_event is not None:
                self._on_event(event)
            return
        if event.channel == "values" and isinstance(event.data, Mapping):
            for content in current_turn_ai_contents(event.data.get("messages", []) or []):
                self._review.check_output(content)
        if self._on_event is not None:
            self._pending.append(event)

    def flush(self, cancel_requested: Event) -> None:
        """只在图成功结束后回放，取消或回调异常时释放尚未交付的事件。"""
        try:
            if self._on_event is None:
                return
            for event in self._pending:
                if cancel_requested.is_set():
                    break
                self._on_event(event)
        finally:
            self.discard()

    def discard(self) -> None:
        """失败或取消后立即释放原生载荷，避免异常堆栈持有整轮缓存。"""
        self._pending.clear()
