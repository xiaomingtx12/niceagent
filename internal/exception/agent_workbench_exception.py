"""Agent 工作台专用异常。"""

from __future__ import annotations


class WorkbenchReplayCursorExpired(Exception):
    """Redis Stream 已裁剪掉请求游标之前的事件。"""

    def __init__(self, oldest_event_sequence: int) -> None:
        self.oldest_event_sequence = oldest_event_sequence
        super().__init__(
            "workbench replay cursor expired before sequence "
            f"{oldest_event_sequence}"
        )


__all__ = ["WorkbenchReplayCursorExpired"]
