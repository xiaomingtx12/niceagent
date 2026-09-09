"""通过 LangGraph 检查点收集用户输入，不占用线程等待表单。"""

from langchain_core.tools import tool
from langgraph.types import interrupt
from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.callbacks import BaseCallbackHandler
from langgraph.errors import GraphInterrupt
from threading import Lock

from internal.core.agent_v2.entity.user_input import UserInputForm


@tool(args_schema=UserInputForm)
def ask_user(title: str, fields: list, description: str = "") -> dict:
    """缺少必要信息时展示表单，相关问题一次收集，已有答案不要重复询问。
    支持 text、textarea、select、multiselect、number；选项可带 value、label、description、recommended，allow_other 开放自由补充。
    skipped/unavailable 表示未提供答案，delegate 仅允许代选该字段；这些状态均不构成敏感操作授权。仅可自主决定的偏好题启用 allow_delegate。
    """
    form = UserInputForm(title=title, description=description, fields=fields)
    response = interrupt(form.model_dump(mode="json"))
    return form.validate_answers(response)


class UserInputMiddleware(AgentMiddleware):
    """仅向主图注入提问工具，避免默认通用子 Agent 继承该能力。"""

    tools = [ask_user]


class UserInputInterruptTracker(BaseCallbackHandler):
    """保留中断异常的类型信息，避免将框架暂停误报为工具失败。"""

    run_inline = True

    def __init__(self):
        """映射仅存活于一次执行，兼容并行工具回调。"""
        self._lock = Lock()
        self._calls = {}
        self._interrupted = set()

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        """只关联提问工具的调用标识，不保存输入正文。"""
        if serialized.get("name") == "ask_user":
            with self._lock:
                self._calls[run_id] = kwargs.get("tool_call_id")

    def on_tool_error(self, error, *, run_id, **kwargs):
        """真实异常仍交给原有错误链路，仅标记 GraphInterrupt。"""
        with self._lock:
            call_id = self._calls.pop(run_id, None)
            if call_id and isinstance(error, GraphInterrupt):
                self._interrupted.add(call_id)

    def on_tool_end(self, output, *, run_id, **kwargs):
        """成功返回后释放回调关联。"""
        with self._lock:
            self._calls.pop(run_id, None)

    def is_interrupted(self, call_id):
        """让事件边界根据实际异常类型识别正常暂停。"""
        with self._lock:
            return call_id in self._interrupted
