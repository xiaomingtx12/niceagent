"""按当前协议的开始、增量、结束顺序合并内容块，供工作台恢复。"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from internal.core.agent_v2.entity.message_event import FieldDelta, MessageEvent, TextDelta
from internal.core.agent_v2.entity.stream_event import EventChannel, MessageEventKind
from internal.schema.agent.agent_protocol_schema import AgentProtocolEvent


@dataclass(frozen=True)
class _ModelCallIdentity:
    """分支、节点和模型调用共同隔离事件；节点直接返回的完整消息可没有模型调用 ID。"""

    namespace: tuple[str, ...]
    node: str | None
    model_run_id: str | None

    @classmethod
    def from_event(cls, event: AgentProtocolEvent) -> "_ModelCallIdentity":
        """只在协议入口读取 metadata，合并过程使用具名身份。"""
        params = event.params
        metadata = params.metadata.get("my_dify", {})
        return cls(params.namespace, params.node, metadata.get("model_run_id"))


@dataclass
class _ContentBlock:
    """分片仅追加，生成快照时才拼接；完整结束帧直接替换累计内容。"""

    start: AgentProtocolEvent
    content: dict[str, Any]
    fragments: dict[str, list[str]] = field(default_factory=dict)

    def append(self, delta: TextDelta | FieldDelta) -> None:
        """结构增量覆盖对应字段时丢弃旧分片，保持当前协议的浅合并语义。"""
        match delta:
            case TextDelta(field=name, value=value, encoding=encoding):
                if name not in self.fragments:
                    self.fragments[name] = [self.content.get(name, "")]
                self.fragments[name].append(value)
                if encoding is not None:
                    self.content["encoding"] = encoding
            case FieldDelta(fields=fields):
                for name in fields:
                    self.fragments.pop(name, None)
                self.content.update(fields)

    def finish(self, content: dict[str, Any]) -> None:
        """结束帧是完整权威内容，无需拼接即将被替换的分片。"""
        self.content = content
        self.fragments.clear()

    def to_event(self) -> AgentProtocolEvent:
        """把最终内容放回真实起点，使水位后到达的增量可以继续追加。"""
        content = {**self.content, **{name: "".join(parts) for name, parts in self.fragments.items()}}
        return _with_data(self.start, {**self.start.params.data, "content": content})


def compact_execution_events(events: Iterable[AgentProtocolEvent]) -> list[AgentProtocolEvent]:
    """合并已提交前缀，保留消息边界和原序号，使恢复后仍可接收后续增量。"""
    kept: dict[int, AgentProtocolEvent | _ContentBlock] = {}
    calls: dict[_ModelCallIdentity, dict[int, _ContentBlock]] = {}
    for event in events:
        if event.method == EventChannel.VALUES:
            event = _with_data(event, {key: value for key, value in event.params.data.items() if key != "messages"})
        elif event.method == EventChannel.MESSAGES:
            message = MessageEvent.parse(event.params.data)
            call = _ModelCallIdentity.from_event(event)
            match message.kind:
                case MessageEventKind.START:
                    calls[call] = {}
                case MessageEventKind.BLOCK_START:
                    assert message.index is not None and message.content is not None
                    block = _ContentBlock(event, dict(message.content))
                    calls[call][message.index] = block
                    kept[event.seq] = block
                    continue
                case MessageEventKind.BLOCK_DELTA:
                    assert message.delta is not None
                    calls[call][message.index].append(message.delta)
                    continue
                case MessageEventKind.BLOCK_FINISH:
                    assert message.content is not None
                    block = calls[call].pop(message.index)
                    block.finish(dict(message.content))
                case MessageEventKind.FINISH | MessageEventKind.ERROR:
                    calls.pop(call, None)
        kept[event.seq] = event
    # 只拷贝最终保留的事件；原始事件与返回快照仍彼此隔离。
    result = []
    for sequence in sorted(kept):
        item = kept[sequence]
        event = item.to_event() if isinstance(item, _ContentBlock) else item
        result.append(event.model_copy(deep=True))
    return result


def _with_data(event: AgentProtocolEvent, data: dict[str, Any]) -> AgentProtocolEvent:
    """只替换载荷，保持原事件的身份、顺序、时间和元数据。"""
    return event.model_copy(update={"params": event.params.model_copy(update={"data": data})})
