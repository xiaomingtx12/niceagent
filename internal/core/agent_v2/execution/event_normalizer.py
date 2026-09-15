"""适配框架 v3 原始事件；消息边界由框架产生，运行终态由 Service 交付。"""

from collections.abc import Iterable, Mapping
from typing import Any

from langchain_core.language_models._compat_bridge import message_to_events
from langchain_core.messages import AIMessageChunk, BaseMessage, ToolMessage
from langgraph.stream import ProtocolEvent

from ..entity.agent_event import AgentEvent
from ..entity.stream_event import CORE_EVENT_CHANNELS, EventChannel
from .json_serialization import to_json_value

class AgentEventNormalizer:
    """单次执行的协议适配器，只保存交付序号，不缓存正文或推断消息终态。"""

    def __init__(self) -> None:
        """完整消息可能展开为多个分片，因此交付序号独立于框架序号。"""
        self._sequence = 0

    def accept(self, raw: ProtocolEvent) -> list[AgentEvent]:
        """输入来自框架的有序 v3 流；只适配公开通道，不重复校验协议外壳。"""
        channel = raw["method"]
        if channel not in CORE_EVENT_CHANNELS:
            return []
        channel = EventChannel(channel)

        params = raw["params"]
        namespace = tuple(params["namespace"])
        timestamp = params.get("timestamp")
        data = params["data"]

        if channel == EventChannel.MESSAGES:
            return self._normalize_messages(data, namespace=namespace, timestamp=timestamp)

        if not isinstance(data, Mapping):
            if channel == EventChannel.CUSTOM:
                return []
            raise ValueError("Agent state and tool events must contain a mapping")
        if channel == EventChannel.LIFECYCLE and not namespace:
            namespace = tuple(data.get("namespace") or ())
        return [self._emit(channel, data, namespace=namespace, timestamp=timestamp)]

    def _normalize_messages(
        self, data: tuple[Any, Mapping[str, Any]], *, namespace: tuple[str, ...], timestamp: Any,
    ) -> list[AgentEvent]:
        """沿用框架 MessagesTransformer 的载荷分支，保留模型调用身份用于并发消息路由。"""
        payload, metadata = data
        if metadata.get("lc_source") == "summarization":
            return []

        message_events: Iterable[Mapping[str, Any]]
        if isinstance(payload, Mapping) and "event" in payload:
            message_events = [payload]
        elif isinstance(payload, BaseMessage) and not isinstance(payload, (AIMessageChunk, ToolMessage)):
            # 按当前 SDK 规则转换节点、缓存返回的完整消息；chunk 和工具消息不重复回放。
            message_events = message_to_events(payload, message_id=payload.id)
        else:
            return []

        node = metadata.get("langgraph_node")
        model_run_id = metadata.get("run_id")
        return [
            self._emit(
                EventChannel.MESSAGES, event, namespace=namespace, timestamp=timestamp,
                node=node, model_run_id=str(model_run_id) if model_run_id is not None else None,
            )
            for event in message_events
        ]

    def _emit(
        self, channel: EventChannel, data: Mapping[str, Any], *, namespace: tuple[str, ...],
        timestamp: Any, node: str | None = None, model_run_id: str | None = None,
    ) -> AgentEvent:
        """出口统一分配交付序号并序列化载荷，业务层无需理解框架消息对象。"""
        self._sequence += 1
        return AgentEvent(
            channel=channel, data=to_json_value(data), sequence=self._sequence,
            namespace=namespace, timestamp=timestamp, node=node, model_run_id=model_run_id,
        )
