"""按当前 langchain-protocol 解释消息：起止帧携带 content，增量帧携带 delta。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .stream_event import ContentType, DeltaKind, MessageEventKind, TOOL_ARGUMENT_CONTENT_TYPES


@dataclass(frozen=True)
class TextDelta:
    """文本、思考和编码数据各自追加到协议指定字段。"""

    field: Literal["text", "reasoning", "data"]
    value: str
    encoding: str | None = None


@dataclass(frozen=True)
class FieldDelta:
    """结构增量覆盖字段，工具参数属于这类增量。"""

    fields: Mapping[str, Any]


@dataclass(frozen=True)
class MessageEvent:
    """已解析的当前协议消息；实时过滤与历史恢复共用同一份解释。"""

    kind: MessageEventKind
    index: int | None = None
    role: str | None = None
    content: Mapping[str, Any] | None = None
    delta: TextDelta | FieldDelta | None = None

    @classmethod
    def parse(cls, payload: Mapping[str, Any]) -> "MessageEvent":
        """按事件类别读取固定字段；非当前格式直接报错，不补字段或猜格式。"""
        kind = MessageEventKind(payload["event"])
        if kind == MessageEventKind.START:
            return cls(kind=kind, role=payload["role"])
        if kind in {MessageEventKind.FINISH, MessageEventKind.ERROR}:
            return cls(kind=kind)
        index = payload["index"]
        if type(index) is not int or index < 0:
            raise ValueError("消息内容块 index 必须为非负整数")
        if kind in {MessageEventKind.BLOCK_START, MessageEventKind.BLOCK_FINISH}:
            content = payload["content"]
            if not isinstance(content, Mapping) or not isinstance(content.get("type"), str):
                raise ValueError("消息起止帧必须包含有效 content")
            return cls(kind=kind, index=index, content=content)
        return cls(kind=kind, index=index, delta=_parse_delta(payload["delta"]))

    @property
    def is_assistant_start(self) -> bool:
        """协议使用 ai 角色，根助手消息在工作台绑定业务回答 ID。"""
        return self.kind == MessageEventKind.START and self.role == "ai"

    @property
    def is_tool_argument_delta(self) -> bool:
        """工具参数只从标准 block-delta.fields 识别，与历史 SQL 一致。"""
        return isinstance(self.delta, FieldDelta) and self.delta.fields["type"] in TOOL_ARGUMENT_CONTENT_TYPES

    @property
    def has_text_delta(self) -> bool:
        """首字只认非空正文增量，思考和工具参数不计入。"""
        return isinstance(self.delta, TextDelta) and self.delta.field == ContentType.TEXT and bool(self.delta.value)


def _parse_delta(value: Any) -> TextDelta | FieldDelta:
    """仅接受当前协议的四种增量，旧 content 分片不进入转换路径。"""
    if not isinstance(value, Mapping):
        raise ValueError("消息增量必须包含有效 delta")
    kind = DeltaKind(value["type"])
    if kind == DeltaKind.FIELDS:
        fields = value["fields"]
        if not isinstance(fields, Mapping) or not isinstance(fields.get("type"), str):
            raise ValueError("结构增量必须包含 fields.type")
        return FieldDelta(fields)
    field: Literal["text", "reasoning", "data"] = {
        DeltaKind.TEXT: "text", DeltaKind.REASONING: "reasoning", DeltaKind.DATA: "data",
    }[kind]
    text = value[field]
    if not isinstance(text, str):
        raise ValueError("文本增量必须为字符串")
    return TextDelta(field, text, value.get("encoding") if kind == DeltaKind.DATA else None)
