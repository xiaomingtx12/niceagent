"""校验增强表单的协议兼容、字段约束及真实工具暂停恢复。"""

from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph, START, END
from langgraph.types import Command

from internal.core.agent_v2.entity.user_input import UserInputField, UserInputForm, UserInputAnswerError
from internal.core.agent_v2.tools.ask_user import ask_user


def field(**kwargs):
    """默认字段含新旧两种选项，验证标签与稳定值分离。"""
    return UserInputField(name="audience", label="读者", type="select", options=[
        "Legacy", {"value": "dev", "label": "开发者", "description": "技术读者", "recommended": True},
    ], **kwargs)


def test_option_values_and_custom_answers_are_distinct():
    """展示标签不能替代选项值，自由补充必须显式启用。"""
    choice = field(allow_other=True)
    assert choice.validate_answer("dev") == "dev"
    assert choice.validate_answer("Legacy") == "Legacy"
    assert choice.validate_answer({"status": "custom", "text": "产品经理"}) == {
        "status": "custom", "text": "产品经理", "selected": [],
    }
    for value in ["开发者", {"status": "custom", "text": " "}, {"status": "custom", "text": "x", "selected": ["dev"]}]:
        with pytest.raises(UserInputAnswerError) as error:
            choice.validate_answer(value)
        assert error.value.field_name == "audience"
    with pytest.raises(ValueError):
        field().validate_answer({"status": "custom", "text": "x"})


@pytest.mark.parametrize("status,setting", [("skipped", "allow_skip"), ("unavailable", "allow_unavailable"), ("delegate", "allow_delegate")])
def test_dispositions_require_their_declared_capability(status, setting):
    """未回答状态保持结构化，不隐式转换成具体值。"""
    assert field(**{setting: True}).validate_answer({"status": status}) == {"status": status}
    with pytest.raises(ValueError):
        field(**{setting: False}).validate_answer({"status": status})
    with pytest.raises(ValueError):
        field(**{setting: True}).validate_answer({"status": status, "value": "injected"})


def test_numeric_text_and_multiselect_constraints():
    """整数、边界和自由补充均在服务端验证，拒绝布尔值、重复及未知选择。"""
    number = UserInputField(name="length", label="字数", type="number", min_value=100, max_value=500, integer_only=True)
    for value in [True, 99, 501, 100.5, float("inf"), "100", 10 ** 1000]:
        with pytest.raises(ValueError):
            number.validate_answer(value)
    assert number.validate_answer(100) == 100
    text = UserInputField(name="topic", label="主题", type="text", min_length=2, max_length=5)
    for value in ["a", "123456"]:
        with pytest.raises(ValueError):
            text.validate_answer(value)
    multiple = UserInputField(name="tags", label="标签", type="multiselect", options=["a", "b"], allow_other=True)
    assert multiple.validate_answer({"status": "custom", "text": "c", "selected": ["a"]})["selected"] == ["a"]
    for selected in [["a", "a"], ["unknown"], [True]]:
        with pytest.raises(ValueError):
            multiple.validate_answer({"status": "custom", "text": "c", "selected": selected})


@pytest.mark.parametrize("definition", [
    {"type": "text", "allow_other": True}, {"type": "text", "integer_only": True},
    {"type": "number", "min_value": 10, "max_value": 1}, {"type": "text", "min_length": 5, "max_length": 2},
    {"type": "select", "options": ["a", {"value": "a", "label": "另一个标签"}]},
])
def test_invalid_field_definitions_are_rejected(definition):
    """模型生成不可渲染或矛盾的字段时立即返回工具校验错误。"""
    with pytest.raises(ValueError):
        UserInputField(name="choice", label="问题", **definition)


def test_real_tool_resume_returns_structured_answers():
    """新答案协议仍通过原生 interrupt 恢复，返回值可再次校验。"""
    form = UserInputForm(title="读者", fields=[field(allow_delegate=True)])

    def ask(state):
        """节点只有表单调用，不在中断前执行外部副作用。"""
        return {"answers": ask_user.invoke(form.model_dump(mode="json"))}

    graph = StateGraph(dict).add_node("ask", ask).add_edge(START, "ask").add_edge("ask", END).compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": str(uuid4())}}
    paused = graph.invoke({}, config)
    assert paused["__interrupt__"][0].value["fields"][0]["options"][1]["recommended"]
    resumed = graph.invoke(Command(resume={"audience": {"status": "delegate"}}), config)
    assert resumed["answers"] == {"audience": {"status": "delegate"}}
    assert form.validate_answers(resumed["answers"]) == resumed["answers"]
