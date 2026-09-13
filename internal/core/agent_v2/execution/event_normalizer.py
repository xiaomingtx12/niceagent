"""在 Core 内解释框架流：规范化消息分片、分支边界及 JSON 载荷。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid4

from langchain_core.language_models._compat_bridge import message_to_events
from langchain_core.messages import AIMessageChunk, BaseMessage, ToolMessage
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from ..entity.agent_event import AgentEvent

_CHANNELS = frozenset({"messages", "values", "tools", "lifecycle", "custom"})
_TERMINAL_EVENTS = frozenset({"completed", "failed", "interrupted", "drained", "cancelled"})


class _FrameworkEvent(BaseModel):
    """只在框架入口检查事件外壳，消息和工具载荷在下方按通道解析。"""

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)
    type: Literal["event"]
    sequence: StrictInt | None = Field(default=None, validation_alias="seq")
    channel: StrictStr = Field(default="custom", validation_alias="method")
    params: Mapping[str, Any] = Field(default_factory=dict)


@dataclass
class _TextStream:
    """仅保存一个分支当前消息的文本块，结束后立即释放。"""

    node: str | None
    index: int = -1
    kind: str = ""
    text: str = ""


class AgentEventNormalizer:
    """单次执行独占框架序号与文本状态；调用方只接收规范化 AgentEvent。"""

    def __init__(self) -> None:
        """规范化序号独立于框架序号，一个框架分片可以产生多个交付事件。"""
        self._last_raw_sequence = 0
        self._sequence = 0
        self._text_streams: dict[tuple[tuple[str, ...], str], _TextStream] = {}

    def accept(self, raw: Mapping[str, Any]) -> list[AgentEvent]:
        """先校验顺序，再处理消息与执行边界；内部通道不向业务层泄漏。"""
        # 1.校验框架事件和源序号，拒绝重复或倒序输入。
        event = _FrameworkEvent.model_validate(raw)
        sequence = event.sequence if event.sequence is not None else self._last_raw_sequence + 1
        if sequence <= self._last_raw_sequence:
            raise ValueError("Agent source event sequence must be strictly increasing")
        self._last_raw_sequence = sequence
        namespace = parse_namespace(event.params.get("namespace"))
        if event.channel not in _CHANNELS:
            return []
        if event.channel == "messages":
            # 2.消息统一进入分片状态机，保持并行分支互不影响。
            return self._normalize_messages(event, namespace)

        data = event.params.get("data")
        if not isinstance(data, Mapping):
            if event.channel == "custom":
                return []
            raise ValueError("Agent state and tool events must contain a mapping")
        timestamp = event.params.get("timestamp")
        if event.channel == "lifecycle" and not namespace:
            namespace = parse_namespace(data.get("namespace"))
        events: list[AgentEvent] = []
        if event.channel == "tools" and data.get("event") == "tool-started":
            # 工具只结束当前分支的模型阶段，不影响并行子图。
            for key in list(self._text_streams):
                if key[0] == namespace:
                    events.extend(self._finish_message(key, timestamp=timestamp))
        if event.channel == "lifecycle" and (data.get("event") or data.get("status")) in _TERMINAL_EVENTS:
            # 3.先封存文本，再发送生命周期边界，避免客户端看到半截消息。
            events.extend(self._finish_messages(timestamp=timestamp, namespace=namespace))
        events.append(self._emit(
            channel=event.channel, data=data, namespace=namespace,
            timestamp=timestamp, node=event.params.get("node"),
        ))
        return events

    def finish(self) -> list[AgentEvent]:
        """执行退出时补齐已交付文本的结束分片，失败或取消也不留下悬空消息。"""
        return self._finish_messages(timestamp=None)

    def _emit(
        self, *, channel: str, data: Mapping[str, Any], namespace: tuple[str, ...],
        timestamp: Any, node: str | None = None,
    ) -> AgentEvent:
        """只在 Core 出口序列化一次，后续业务适配无需理解框架类型。"""
        self._sequence += 1
        return AgentEvent(
            channel=channel, data=json_value(data), sequence=self._sequence,
            namespace=tuple(namespace), timestamp=timestamp, node=node,
        )

    def _normalize_messages(self, event: _FrameworkEvent, namespace: tuple[str, ...]) -> list[AgentEvent]:
        """模型消息只转换公开分片；摘要调用不作为用户可见回答。"""
        # 1.拆出消息载荷和框架 metadata，兼容元组与直接消息格式。
        timestamp = event.params.get("timestamp")
        data = event.params.get("data")
        payload = data
        metadata: Mapping[str, Any] = {}
        if isinstance(data, (list, tuple)) and len(data) == 2:
            payload = data[0]
            if isinstance(data[1], Mapping):
                metadata = data[1]
        # 摘要用于框架内部上下文压缩，不属于助手回答。
        if metadata.get("lc_source") == "summarization":
            return []
        node_value = metadata.get("langgraph_node")
        node = node_value if isinstance(node_value, str) and node_value else event.params.get("node")
        if isinstance(payload, Mapping) and payload.get("event"):
            message_events = [payload]
        elif isinstance(payload, AIMessageChunk):
            return self._normalize_chunk(
                payload,
                namespace=namespace,
                timestamp=timestamp,
                node=node,
            )
        elif isinstance(payload, BaseMessage) and not isinstance(
            payload, (AIMessageChunk, ToolMessage)
        ):
            message_events = list(message_to_events(payload, message_id=payload.id))
        else:
            return []
        for message_event in message_events:
            # 2.跟踪直接分片，取消或异常时也能补齐结束事件。
            self._track_message(message_event, namespace=namespace, node=node)
        return [
            self._emit(
                channel="messages",
                namespace=namespace,
                timestamp=timestamp,
                data=message_event,
                node=node,
            )
            for message_event in message_events
        ]

    def _track_message(
        self, data: Mapping[str, Any], *, namespace: tuple[str, ...], node: str | None,
    ) -> None:
        """原生协议分片也可能在取消时被截断，保留其未结束文本用于统一收尾。"""
        key = (namespace, node or "")
        event = data.get("event")
        if event == "message-start":
            self._text_streams[key] = _TextStream(node=node)
        elif event == "message-finish":
            self._text_streams.pop(key, None)
        else:
            state = self._text_streams.get(key)
            if state is None:
                return
            if event == "content-block-start":
                content = data.get("content") or {}
                kind = content.get("type")
                if kind in {"text", "reasoning"}:
                    state.index, state.kind = data["index"], kind
                    state.text = content.get(kind, "")
            elif event == "content-block-delta" and state.kind:
                delta = data.get("delta") or {}
                state.text += delta.get(state.kind, "")
            elif event == "content-block-finish":
                state.kind, state.text = "", ""

    def _normalize_chunk(
        self,
        chunk: AIMessageChunk,
        *,
        namespace: tuple[str, ...],
        timestamp: Any,
        node: str | None,
    ) -> list[AgentEvent]:
        """只投影标准化的公开正文和推理文本，签名、工具参数不进入展示内容。"""
        # 1.按 namespace 和节点确定独立文本流。
        stream_hint = node or (str(chunk.id or "") if not namespace else "")
        key = (namespace, stream_hint)
        state = self._text_streams.get(key)
        events: list[AgentEvent] = []
        for block in chunk.content_blocks:
            kind = block.get("type")
            if kind not in {"text", "reasoning"}:
                continue
            value = block.get(kind)
            if not isinstance(value, str) or not value:
                continue
            if state is None:
                # 2.首次收到内容时创建消息和首个内容块。
                state = _TextStream(node=node)
                self._text_streams[key] = state
                events.append(
                    self._emit(
                        channel="messages", namespace=namespace, timestamp=timestamp,
                        node=node,
                        data={"event": "message-start", "role": "ai", "id": str(chunk.id or uuid4())},
                    )
                )
            if state.kind != kind:
                # 3.内容类型切换时先关闭旧块，再开始新块。
                if state.index >= 0:
                    events.append(self._emit(
                        channel="messages", namespace=namespace, timestamp=timestamp,
                        node=node,
                        data={"event": "content-block-finish", "index": state.index,
                              "content": {"type": state.kind, state.kind: state.text}},
                    ))
                state.index += 1
                state.kind, state.text = kind, ""
                events.append(self._emit(
                    channel="messages", namespace=namespace, timestamp=timestamp,
                    node=node,
                    data={"event": "content-block-start", "index": state.index,
                          "content": {"type": kind, kind: ""}},
                ))
            state.text += value
            events.append(
                self._emit(
                    channel="messages",
                    namespace=namespace,
                    timestamp=timestamp,
                    node=node,
                    data={
                        "event": "content-block-delta",
                        "index": state.index,
                        "delta": {"type": f"{kind}-delta", kind: value},
                    },
                )
            )
        terminal = (
            chunk.chunk_position == "last"
            or bool((chunk.response_metadata or {}).get("finish_reason"))
            or bool((chunk.response_metadata or {}).get("stop_reason"))
        )
        if terminal and key in self._text_streams:
            # 4.供应商显式结束时立即释放流状态。
            events.extend(
                self._finish_message(
                    key,
                    timestamp=timestamp,
                )
            )
        return events

    def _finish_message(
        self,
        key: tuple[tuple[str, ...], str],
        *,
        timestamp: Any,
    ) -> list[AgentEvent]:
        """移除文本流并一次补齐正文和消息结束事件，重复关闭不产生事件。"""
        state = self._text_streams.pop(key, None)
        if state is None:
            return []
        events = []
        if state.kind:
            events.append(self._emit(
                channel="messages",
                namespace=key[0],
                timestamp=timestamp,
                node=state.node,
                data={
                    "event": "content-block-finish",
                    "index": state.index,
                    "content": {"type": state.kind, state.kind: state.text},
                },
            ))
        events.append(self._emit(
            channel="messages",
            namespace=key[0],
            timestamp=timestamp,
            node=state.node,
            data={"event": "message-finish"},
        ))
        return events

    def _finish_messages(
        self,
        *,
        timestamp: Any,
        namespace: tuple[str, ...] | None = None,
    ) -> list[AgentEvent]:
        """关闭指定 namespace 子树中的文本流；根路径关闭全部。"""
        events: list[AgentEvent] = []
        for key in list(self._text_streams):
            stream_namespace = key[0]
            if (
                namespace is not None
                and stream_namespace[: len(namespace)] != namespace
            ):
                continue
            events.extend(
                self._finish_message(
                    key,
                    timestamp=timestamp,
                )
            )
        return events


def json_value(value: Any) -> Any:
    """把框架对象转换为独立于供应商对象的 JSON 值。"""
    if isinstance(value, BaseMessage):
        try:
            return value.model_dump(mode="json")
        except (TypeError, ValueError):
            return {
                "type": value.type,
                "content": json_value(value.content),
                "id": value.id,
            }
    if isinstance(value, Mapping):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_value(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return json_value(model_dump(mode="json"))
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def parse_namespace(value: Any) -> tuple[str, ...]:
    """图路径必须是字符串数组，拒绝隐式转换导致分支身份混淆。"""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(part, str) for part in value):
        raise ValueError("Agent event namespace must contain strings")
    return tuple(value)
