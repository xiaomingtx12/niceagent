"""DeepAgents 运行时的业务事件翻译工具。"""

from importlib import import_module

_EXPORTS = {
    "EventTranslationSession": (
        "internal.core.agent_v2.events.session",
        "EventTranslationSession",
    ),
    "bind_event_translation": (
        "internal.core.agent_v2.events.session",
        "bind_event_translation",
    ),
    "is_current_run_cancelled": (
        "internal.core.agent_v2.events.session",
        "is_current_run_cancelled",
    ),
}


def __getattr__(name: str):
    """按需加载事件对象，避免事件包初始化依赖执行层。"""
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    return getattr(import_module(module_name), attribute)


__all__ = list(_EXPORTS)
