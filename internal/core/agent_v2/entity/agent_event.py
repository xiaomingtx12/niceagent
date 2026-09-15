"""Core 交付的规范化事件，不携带框架消息对象、业务 ID 或持久化游标。"""

from dataclasses import dataclass, field
from typing import Any

from .message_event import MessageEvent
from .stream_event import EventChannel, LifecycleEvent


@dataclass(frozen=True)
class AgentEvent:
    """sequence 为交付序号；namespace 标识分支，model_run_id 区分同一节点内的模型调用。"""

    channel: EventChannel
    data: dict[str, Any]
    sequence: int
    namespace: tuple[str, ...] = ()
    timestamp: int | float | None = None
    node: str | None = None
    model_run_id: str | None = None
    message: MessageEvent | None = field(init=False, default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        """消息字段在 Core 出口解析一次，下游直接使用具名语义。"""
        if self.channel == EventChannel.MESSAGES:
            object.__setattr__(self, "message", MessageEvent.parse(self.data))

    @property
    def is_root_terminal(self) -> bool:
        """根图结束只代表框架已退出，业务提交成功后才能通知客户端结束。"""
        return (
            self.channel == EventChannel.LIFECYCLE
            and not self.namespace
            and self.data.get("event") in LifecycleEvent
        )
