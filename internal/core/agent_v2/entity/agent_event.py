"""Agent Core 的执行事件，不包含工作台游标或业务消息标识。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentEvent:
    """保留模型原生载荷；传输序号、序列化和前端消息投影由调用方负责。"""

    channel: str
    data: Any
    namespace: tuple[str, ...] = ()
    sequence: int | None = None
    timestamp: Any = None
    node: str | None = None

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "AgentEvent":
        """在框架边界验证事件外壳，工具和模型载荷仍保持原生对象。"""
        if raw.get("type") != "event":
            raise ValueError("Agent event type must be 'event'")
        sequence = raw.get("seq")
        if sequence is not None and (
            isinstance(sequence, bool) or not isinstance(sequence, int)
        ):
            raise ValueError("Agent event sequence must be an integer")
        params = raw.get("params") or {}
        if not isinstance(params, Mapping):
            raise ValueError("Agent event params must be an object")
        namespace = params.get("namespace")
        if namespace is None:
            namespace = ()
        if not isinstance(namespace, (list, tuple)) or not all(
            isinstance(part, str) for part in namespace
        ):
            raise ValueError("Agent event namespace must contain strings")
        channel = raw.get("method")
        if channel is not None and not isinstance(channel, str):
            raise ValueError("Agent event channel must be a string")
        return cls(
            channel=channel or "custom",
            data=params.get("data"),
            namespace=tuple(namespace),
            sequence=sequence,
            timestamp=params.get("timestamp"),
            node=params.get("node"),
        )
