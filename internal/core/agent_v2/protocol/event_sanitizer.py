"""工作台协议事件的固定安全投影。

这个模块只负责工作台的出站安全边界，不负责决定 Agent 如何执行。
Todo、工具、Skill 和子 Agent 事件分别通过 ``values``、``tools`` 和
``lifecycle`` 通道保留；这里只删除不应进入浏览器的内部内容。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_PUBLIC_METHODS = frozenset({"messages", "values", "tools", "lifecycle"})
_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "access_token",
        "authorization",
        "cookie",
        "client_secret",
        "connection_string",
        "credential",
        "credentials",
        "database_url",
        "headers",
        "password",
        "private_key",
        "refresh_token",
        "secret",
        "secret_key",
        "set_cookie",
        "stack",
        "stacktrace",
        "system_prompt",
        "token",
        "traceback",
    }
)


def _normalized_key(key: Any) -> str:
    """统一敏感字段命名，兼容 ``client-secret`` 等外部协议写法。"""
    return str(key).strip().lower().replace("-", "_").replace(" ", "_")


def _is_sensitive_key(key: Any) -> bool:
    return _normalized_key(key) in _SENSITIVE_KEYS


def sanitize_workbench_event(
    event: Mapping[str, Any],
) -> dict[str, Any] | None:
    """投影统一工作台事件。

    工作台当前公开四个稳定通道：``messages``、``values``、``tools``、
    ``lifecycle``。通道内的工具、Todo 和子 Agent 事件不会被隐藏；只有
    不属于工作台协议的 channel 会被丢弃，字段内容统一递归脱敏。
    """
    # 1.先限制公开通道，再递归裁剪敏感字段，避免未知 channel 泄露内部事件。
    method_value = event.get("method")
    method = method_value if isinstance(method_value, str) else ""
    if method not in _PUBLIC_METHODS:
        return None

    projected = _redact(event)
    params = projected.get("params")
    if not isinstance(params, dict):
        return projected
    # 2.values 只保留工作台需要的状态；messages 过滤 system 内容但保留工具消息。
    data = params.get("data")
    if method == "values" and isinstance(data, Mapping):
        params["data"] = {
            key: _filter_conversation_messages(value) if key == "messages" else value
            for key, value in data.items()
            if key in {"messages", "todos", "files"}
        }
    elif method == "messages":
        params["data"] = _filter_conversation_messages(data)
    return projected


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): ("[REDACTED]" if _is_sensitive_key(key) else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_redact(item) for item in value]
    return value


def _filter_conversation_messages(value: Any) -> Any:
    """过滤 system 消息，保留 tool 消息供工作台执行详情使用。

    工具调用已经是工作台的一等事件，完整的调用状态由 ``tools`` 通道
    负责展示；如果它同时出现在 ``values.messages`` 快照中，也不能在
    服务端静默删除，否则断线恢复后的执行详情会和实时流不一致。
    """
    if isinstance(value, list):
        return [
            projected
            for item in value
            if (projected := _filter_conversation_messages(item)) is not None
        ]
    if isinstance(value, Mapping):
        message_type = str(value.get("type") or value.get("role") or "").lower()
        if message_type == "system":
            return None
        return {
            str(key): _filter_conversation_messages(item) for key, item in value.items()
        }
    return value


__all__ = ["sanitize_workbench_event"]
