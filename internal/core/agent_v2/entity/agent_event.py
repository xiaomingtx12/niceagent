"""Core 交付的规范化事件，不携带框架消息对象、业务 ID 或持久化游标。"""

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class AgentEvent:
    """data 为 JSON 兼容载荷；sequence 在一次执行内递增，namespace 标识独立执行分支。"""

    channel: Literal["messages", "values", "tools", "lifecycle", "custom"]
    data: dict[str, Any]
    sequence: int
    namespace: tuple[str, ...] = ()
    timestamp: int | float | None = None
    node: str | None = None
