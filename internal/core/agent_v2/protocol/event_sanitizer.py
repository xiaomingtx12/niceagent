"""工作台协议事件的公开通道投影。

流式事件的内容由 Agent Protocol 负责承载，工作台不在这里猜测或改写
业务数据。这个模块只保留工作台支持的公开通道；具体事件的转换、编号和
生命周期整理由 ``event_projector`` 完成。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_PUBLIC_METHODS = frozenset({"messages", "values", "tools", "lifecycle"})


def project_workbench_event(
    event: Mapping[str, Any],
) -> dict[str, Any] | None:
    """投影到工作台公开通道，并原样返回事件内容。

    这里不做敏感字段、system 消息、工具输出或 Skill 正文的内容级修剪。
    调用方如需限制订阅范围，应使用 ``WorkbenchSubscriptionReq`` 的
    ``channels``、``namespaces`` 和 ``depth`` 条件，而不是修改事件数据。
    """
    method = event.get("method")
    if not isinstance(method, str) or method not in _PUBLIC_METHODS:
        return None
    return dict(event)


def sanitize_workbench_event(
    event: Mapping[str, Any],
) -> dict[str, Any] | None:
    """兼容旧调用方的公开通道投影别名。"""
    return project_workbench_event(event)


__all__ = ["project_workbench_event", "sanitize_workbench_event"]
