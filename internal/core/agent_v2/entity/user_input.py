"""Agent 提问协议及答案规则，供工具和提交边界共同使用。"""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictStr, model_validator


class UserInputField(BaseModel):
    """限制可渲染控件和数据规模，不允许模型注入页面代码。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=200)
    type: Literal["text", "textarea", "select", "multiselect", "number"]
    required: StrictBool = True
    options: list[StrictStr] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_options(self) -> "UserInputField":
        """选择控件必须有不重复的选项，其余控件不接受选项。"""
        if self.type in {"select", "multiselect"}:
            if not self.options or len(set(self.options)) != len(self.options):
                raise ValueError("选项不能为空或重复")
            if any(not option.strip() or len(option) > 200 for option in self.options):
                raise ValueError("选项必须是 1 至 200 字的文本")
        elif self.options:
            raise ValueError("只有选择控件支持 options")
        return self


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
            value = answers.get(field.name)
            empty = value is None or value == "" or value == [] or (
                isinstance(value, str) and not value.strip()
            )
            if empty:
                if field.required:
                    raise ValueError(f"请填写：{field.label}")
                continue
            valid = False
            if field.type in {"text", "textarea"}:
                valid = isinstance(value, str) and len(value) <= 4000
            elif field.type == "number":
                valid = type(value) in {int, float} and abs(value) <= 1e15 and math.isfinite(value)
            elif field.type == "select":
                valid = isinstance(value, str) and value in field.options
            elif field.type == "multiselect":
                valid = (
                    isinstance(value, list)
                    and all(isinstance(item, str) and item in field.options for item in value)
                    and len(value) == len(set(value))
                )
            if not valid:
                raise ValueError(f"答案格式不正确：{field.label}")
            result[field.name] = value
        return result


class PendingUserInput(BaseModel):
    """持久化的框架中断身份及经过校验的公开表单。"""

    model_config = ConfigDict(extra="forbid")

    interrupt_id: str = Field(min_length=1, max_length=255)
    namespace: list[str] = Field(default_factory=list)
    payload: UserInputForm
