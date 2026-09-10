from wtforms import Field
from wtforms.validators import StopValidation


class JsonString:
    """要求 WTForms 字段接收的原始 JSON 值为字符串。"""

    def __init__(self, message: str = "必须是字符串") -> None:
        """保存字段类型错误文案。"""
        self.message = message

    def __call__(self, form, field) -> None:
        """在后续字符串 validator 执行前拒绝非字符串原始值。"""
        value = (
            field.raw_data[0]
            if field.raw_data is not None and field.raw_data
            else field.object_data
        )
        if value is not None and not isinstance(value, str):
            raise StopValidation(self.message)


class JsonBoolean:
    """要求 WTForms 字段接收的原始 JSON 值为布尔值。"""

    def __init__(
        self,
        message: str = "必须是布尔值",
        *,
        allow_none: bool = False,
    ) -> None:
        """保存字段类型错误文案。"""
        self.message = message
        self.allow_none = allow_none

    def __call__(self, form, field) -> None:
        """拒绝会被 BooleanField 隐式转换的字符串和数字。"""
        value = (
            field.raw_data[0]
            if field.raw_data is not None and field.raw_data
            else field.object_data
        )
        if value is None and self.allow_none:
            return
        if not isinstance(value, bool):
            raise StopValidation(self.message)
        field.data = value


class ListField(Field):
    """自定义list字段，用于存储列表型数据"""

    data: list = None

    def process_formdata(self, valuelist):
        if valuelist is not None and isinstance(valuelist, list):
            self.data = valuelist

    def _value(self):
        return self.data if self.data else []


class DictField(Field):
    """自定义dict字段"""

    data: dict = None

    def process_formdata(self, valuelist):
        if (
            valuelist is not None
            and len(valuelist) > 0
            and isinstance(valuelist[0], dict)
        ):
            self.data = valuelist[0]

    def _value(self):
        return self.data
