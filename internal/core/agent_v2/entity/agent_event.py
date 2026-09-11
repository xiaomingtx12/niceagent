"""Agent Core 的执行事件，不包含工作台游标或业务消息标识。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError


class _RawAgentEvent(BaseModel):
    """只校验框架事件外壳，保留 data 中的模型和工具对象。"""

    model_config = ConfigDict(
        extra="ignore",
        arbitrary_types_allowed=True,
    )

    type: StrictStr
    sequence: StrictInt | None = Field(default=None, validation_alias="seq")
    channel: StrictStr | None = Field(default=None, validation_alias="method")
    params: Mapping[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class AgentEvent:
    """表示一次 Core 执行事件，不承担工作台协议投影和持久化职责。"""

    channel: str
    data: Any
    namespace: tuple[str, ...] = ()
    sequence: int | None = None
    timestamp: Any = None
    node: str | None = None

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "AgentEvent":
        """在框架边界校验事件外壳，保留工具和模型载荷的原生对象。"""
        try:
            event = _RawAgentEvent.model_validate(raw)
        except ValidationError as exc:
            raise ValueError("Agent event envelope is invalid") from exc
        if event.type != "event":
            raise ValueError("Agent event type must be 'event'")

        # 1.params 由 Pydantic 校验为映射；data 仍保留框架传入的原生对象。
        params = event.params

        # 2.namespace 决定事件路由，非法值在 Core 边界立即拒绝。
        namespace = cls._parse_namespace(params.get("namespace"))

        return cls(
            channel=event.channel or "custom",
            data=params.get("data"),
            namespace=tuple(namespace),
            sequence=event.sequence,
            timestamp=params.get("timestamp"),
            node=params.get("node"),
        )

    @staticmethod
    def _parse_namespace(value: Any) -> tuple[str, ...]:
        """校验图路径并统一为不可变元组，避免事件对象被外部列表修改。"""
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("Agent event namespace must contain strings")
        if not all(isinstance(part, str) for part in value):
            raise ValueError("Agent event namespace must contain strings")
        return tuple(value)

    @staticmethod
    def _parse_channel(value: Any) -> str | None:
        """校验事件通道；缺省通道仍由入口兼容为 custom。"""
        if value is not None and not isinstance(value, str):
            raise ValueError("Agent event channel must be a string")
        return value
