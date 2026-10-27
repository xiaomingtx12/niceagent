"""通过 LangGraph 检查点收集用户输入，不占用线程等待表单。"""

from langchain_core.tools import tool
from langgraph.types import interrupt

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
