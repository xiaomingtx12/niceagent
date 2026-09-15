"""Core 交付的规范化事件，不携带框架消息对象、业务 ID 或持久化游标。"""

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)  # 使用frozen=True创建一个不可变的数据类
class AgentEvent:
    """sequence 为交付序号；namespace 标识分支，model_run_id 区分同一节点内的模型调用。"""

    channel: Literal["messages", "values", "tools", "lifecycle", "custom"]
    data: dict[str, Any]
    sequence: int
    namespace: tuple[str, ...] = ()
    timestamp: int | float | None = None
    node: str | None = None
    model_run_id: str | None = None
