"""在 Core 出口将事件载荷和执行状态转成 JSON 兼容值，不生成 JSON 字符串。"""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from langchain_core.messages import BaseMessage


def to_json_value(value: Any) -> Any:
    """递归转换框架对象；消息序列化失败时保留基本字段，其他未知对象转为字符串。"""
    if isinstance(value, BaseMessage):
        try:
            return value.model_dump(mode="json")
        except (TypeError, ValueError):
            return {
                "type": value.type,
                "content": to_json_value(value.content),
                "id": value.id,
            }
    if isinstance(value, Mapping):
        return {str(key): to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_json_value(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return to_json_value(model_dump(mode="json"))
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


__all__ = ["to_json_value"]
