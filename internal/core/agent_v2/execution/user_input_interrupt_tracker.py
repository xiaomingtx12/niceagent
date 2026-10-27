"""通过工具回调识别用户输入暂停，供事件交付过滤正常中断。"""

from threading import Lock
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langgraph.errors import GraphInterrupt


class UserInputInterruptTracker(BaseCallbackHandler):
    """保留中断异常的类型信息，避免将框架暂停误报为工具失败。"""

    run_inline = True

    def __init__(self) -> None:
        """映射仅存活于一次执行，兼容并行工具回调。"""
        self._lock = Lock()
        super().__init__()
        self._calls: dict[UUID, str | None] = {}
        self._interrupted: set[str] = set()

    def on_tool_start(self, serialized: dict[str, Any], input_str: str, *, run_id: UUID, **kwargs: Any) -> None:
        """只关联提问工具的调用标识，不保存输入正文。"""
        if serialized.get("name") == "ask_user":
            with self._lock:
                self._calls[run_id] = kwargs.get("tool_call_id")

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """真实异常仍交给原有错误链路，仅标记 GraphInterrupt。"""
        with self._lock:
            call_id = self._calls.pop(run_id, None)
            if call_id and isinstance(error, GraphInterrupt):
                self._interrupted.add(call_id)

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        """成功返回后释放回调关联。"""
        with self._lock:
            self._calls.pop(run_id, None)

    def is_interrupted(self, call_id: str | None) -> bool:
        """让事件边界根据实际异常类型识别正常暂停。"""
        with self._lock:
            return call_id in self._interrupted
