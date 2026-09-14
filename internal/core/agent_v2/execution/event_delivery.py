"""图事件的规范化交付，不参与图执行及 Backend 生命周期。"""

from collections.abc import Callable, Mapping
from typing import Any

from internal.core.agent_v2.entity.agent_event import AgentEvent
from internal.core.agent_v2.execution.event_normalizer import AgentEventNormalizer


class AgentEventDelivery:
    """在 Core 出口规范化事件后交付，业务订阅者无需处理框架对象。"""

    def __init__(self, on_event: Callable[[AgentEvent], None] | None) -> None:
        """交付缓冲只属于一次 execute；没有订阅者时事件直接丢弃。"""
        self._on_event = on_event
        self._normalizer = AgentEventNormalizer()

    def accept(self, raw: Mapping[str, Any]) -> None:
        """一次框架事件可拆成多个标准分片，按顺序直接交给订阅者。"""
        if self._on_event is not None:
            for event in self._normalizer.accept(raw):
                self._on_event(event)

    def finish(self) -> None:
        """图退出后补齐尚未收到结束分片的消息，失败或取消也不留下悬空消息。"""
        if self._on_event is not None:
            for event in self._normalizer.finish():
                self._on_event(event)


__all__ = ["AgentEventDelivery"]
