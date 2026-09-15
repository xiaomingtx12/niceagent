"""Agent 提问协议及答案规则，供工具和提交边界共同使用。"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator


class UserInputOption(BaseModel):
    """选项值用于恢复执行，标签和说明只负责展示。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    value: StrictStr = Field(min_length=1, max_length=200)
    label: StrictStr = Field(min_length=1, max_length=200)
    description: StrictStr = Field(default="", max_length=500)
    recommended: StrictBool = False


class UserInputAnswerError(ValueError):
    """保留稳定字段名，便于提交边界定位错误而不依赖展示文案。"""

    def __init__(self, field_name: str, message: str):
        """只携带定位信息，不记录用户答案。"""
        self.field_name = field_name
        super().__init__(message)


class UserInputField(BaseModel):
    """限制可渲染控件和数据规模，不允许模型注入页面代码。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=200)
    type: Literal["text", "textarea", "select", "multiselect", "number"]
    required: StrictBool = True
    options: list[StrictStr | UserInputOption] = Field(default_factory=list, max_length=20)
    description: str = Field(default="", max_length=500)
    placeholder: str = Field(default="", max_length=200)
    allow_other: StrictBool = False
    allow_skip: StrictBool = False
    allow_unavailable: StrictBool = True
    allow_delegate: StrictBool = False
    min_length: int = Field(default=0, ge=0, le=4000, strict=True)
    max_length: int = Field(default=4000, ge=1, le=4000, strict=True)
    min_value: float | None = Field(default=None, ge=-1e15, le=1e15, allow_inf_nan=False, strict=True)
    max_value: float | None = Field(default=None, ge=-1e15, le=1e15, allow_inf_nan=False, strict=True)
    integer_only: StrictBool = False

    @model_validator(mode="after")
    def validate_options(self) -> "UserInputField":
        """选择控件必须有不重复的选项，其余控件不接受选项。"""
        if self.type in {"select", "multiselect"}:
            values = self.option_values
            if not values or len(set(values)) != len(values):
                raise ValueError("选项不能为空或重复")
            if any(not option.strip() or len(option) > 200 for option in values):
                raise ValueError("选项必须是 1 至 200 字的文本")
            if sum(isinstance(option, UserInputOption) and option.recommended for option in self.options) > 1:
                raise ValueError("每题最多标记一个推荐选项")
        elif self.options or self.allow_other:
            raise ValueError("只有选择控件支持 options 和 allow_other")
        if self.min_length > self.max_length:
            raise ValueError("最短长度不能超过最长长度")
        if self.min_value is not None and self.max_value is not None and self.min_value > self.max_value:
            raise ValueError("最小值不能超过最大值")
        if self.type != "number" and (self.min_value is not None or self.max_value is not None or self.integer_only):
            raise ValueError("只有数字控件支持数值约束")
        return self

    @property
    def option_values(self) -> list[str]:
        """兼容已保存的字符串选项，拒绝把展示标签当作稳定值。"""
        return [option if isinstance(option, str) else option.value for option in self.options]

    def validate_answer(self, value: Any) -> Any:
        """结构化状态不能冒充普通答案；自由补充与已有选项分别验证。"""
        if isinstance(value, dict):
            status = value.get("status")
            allowed = {"skipped": self.allow_skip or not self.required,
                       "unavailable": self.allow_unavailable, "delegate": self.allow_delegate}
            if isinstance(status, str) and status in allowed and allowed[status] and set(value) == {"status"}:
                return dict(value)
            if status == "custom" and self.allow_other and set(value) <= {"status", "text", "selected"}:
                text = value.get("text")
                selected = value.get("selected", [])
                if (isinstance(text, str) and text.strip() and self.min_length <= len(text) <= self.max_length
                        and isinstance(selected, list) and all(isinstance(item, str) and item in self.option_values for item in selected)
                        and len(selected) == len(set(selected)) and (self.type == "multiselect" or not selected)):
                    return {"status": "custom", "text": text, "selected": selected}
            raise UserInputAnswerError(self.name, f"答案格式不正确：{self.label}")
        empty = value is None or value == "" or value == [] or (isinstance(value, str) and not value.strip())
        if empty:
            if self.required:
                raise UserInputAnswerError(self.name, f"请填写：{self.label}")
            return None
        valid = False
        if self.type in {"text", "textarea"}:
            valid = isinstance(value, str) and self.min_length <= len(value) <= self.max_length
        elif self.type == "number":
            valid = (type(value) in {int, float} and abs(value) <= 1e15 and math.isfinite(value)
                     and (self.min_value is None or value >= self.min_value)
                     and (self.max_value is None or value <= self.max_value)
                     and (not self.integer_only or value % 1 == 0))
        elif self.type == "select":
            valid = isinstance(value, str) and value in self.option_values
        elif self.type == "multiselect":
            valid = (isinstance(value, list) and all(isinstance(item, str) and item in self.option_values for item in value)
                     and len(value) == len(set(value)))
        if not valid:
            raise UserInputAnswerError(self.name, f"答案格式不正确：{self.label}")
        return value


class UserInputForm(BaseModel):
    """一次提问收集多个必要字段，稳定字段名用于恢复执行。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)
    fields: list[UserInputField] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def validate_names(self) -> "UserInputForm":
        """拒绝同名字段，避免前端覆盖答案。"""
        if len({field.name for field in self.fields}) != len(self.fields):
            raise ValueError("表单字段名不能重复")
        return self

    def validate_answers(self, answers: Any) -> dict[str, Any]:
        """按服务端保存的表单验证答案，拒绝未知字段和隐式类型转换。"""
        if not isinstance(answers, dict) or set(answers) - {f.name for f in self.fields}:
            raise ValueError("答案包含未知字段或不是对象")
        result = {}
        for field in self.fields:
            value = field.validate_answer(answers.get(field.name))
            if value is not None:
                result[field.name] = value
        return result


class PendingUserInput(BaseModel):
    """持久化的框架中断身份及经过校验的公开表单。"""

    model_config = ConfigDict(extra="forbid")

    interrupt_id: str = Field(min_length=1, max_length=255)
    namespace: list[str] = Field(default_factory=list)
    payload: UserInputForm
