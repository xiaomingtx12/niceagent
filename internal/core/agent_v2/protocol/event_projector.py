"""将 Deep Agents v3 原始事件投影为 Agent Protocol 传输结构。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from langchain_core.language_models._compat_bridge import message_to_events
from langchain_core.messages import AIMessageChunk, BaseMessage, ToolMessage

from internal.schema.agent_workbench_schema import WorkbenchEvent
from .event_sanitizer import sanitize_workbench_event

_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}
_TERMINAL_LIFECYCLE_EVENTS = frozenset(
    {"completed", "failed", "interrupted", "drained", "cancelled"}
)


def _json_value(value: Any) -> Any:
    """把框架对象转换为可进入 Agent Protocol 的 JSON 值。"""
    if isinstance(value, BaseMessage):
        try:
            return value.model_dump(mode="json")
        except (TypeError, ValueError):
            return {
                "type": value.type,
                "content": value.content,
                "id": value.id,
            }
    if isinstance(value, Mapping):
        return {
            str(key): (
                "[REDACTED]"
                if str(key).lower() in _SENSITIVE_KEYS
                else _json_value(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if isinstance(value, UUID):
        return str(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_value(model_dump(mode="json"))
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _normalize_namespace(value: Any) -> tuple[str, ...]:
    """校验 v3 事件的图路径，不隐式转换非法元素。"""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("protocol event namespace must be an array")
    if not all(isinstance(item, str) for item in value):
        raise ValueError("protocol event namespace items must be strings")
    return tuple(value)


class AgentProtocolEventProjector:
    """把一次 DeepAgents v3 运行转换成有序的 Agent Protocol 事件流。

    本类只负责事件编号、消息分片合并和协议形状，不负责数据库、Redis
    或 SSE；出站安全裁剪由 ``event_sanitizer`` 统一完成。
    """

    def __init__(
        self,
        run_id: UUID | str | None = None,
        *,
        start_sequence: int = 0,
        root_message_id: str | None = None,
    ) -> None:
        self.run_id = str(run_id or uuid4())
        if start_sequence < 0:
            raise ValueError("protocol start sequence must be non-negative")
        self.last_event_sequence = start_sequence
        self._last_raw_sequence = 0
        self._root_message_id = root_message_id
        self._text_streams: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}

    @staticmethod
    def to_public_event(
        event: Mapping[str, Any] | WorkbenchEvent,
    ) -> WorkbenchEvent | None:
        """完成 Agent Protocol 出站脱敏、通道过滤和结构校验。"""
        raw_event = _json_value(
            event.model_dump(mode="python")
            if isinstance(event, WorkbenchEvent)
            else event
        )
        visible = sanitize_workbench_event(raw_event)
        return WorkbenchEvent.model_validate(visible) if visible is not None else None

    def _event(
        self,
        *,
        method: str,
        namespace: Any,
        timestamp: Any,
        data: Any,
        node: str | None = None,
        source_sequence: int | None = None,
        source: str = "deepagents",
    ) -> dict[str, Any]:
        """分配序号并创建统一事件外壳。"""
        self.last_event_sequence += 1
        params = {
            "namespace": _json_value(namespace or []),
            "timestamp": timestamp,
            "data": _json_value(data),
        }
        if node:
            params["node"] = node
        params["metadata"] = {
            "my_dify": {
                "source": source,
                **(
                    {"source_seq": source_sequence}
                    if source_sequence is not None
                    else {}
                ),
            }
        }
        return {
            "type": "event",
            "event_id": f"{self.run_id}:{self.last_event_sequence}",
            "seq": self.last_event_sequence,
            "method": method,
            "params": params,
        }

    def generated(
        self,
        method: str,
        data: Mapping[str, Any],
        *,
        namespace: list[str] | None = None,
    ) -> dict[str, Any]:
        """生成由工作台运行边界补充的协议事件。"""
        return self._event(
            method=method,
            namespace=namespace or [],
            timestamp=None,
            data=data,
            source="workbench",
        )

    def finish_pending_messages(self) -> list[dict[str, Any]]:
        """在运行结束边界关闭未携带 provider finish 标记的文本流。"""
        return self._finish_text_streams(timestamp=None, source="workbench")

    def project_many(self, raw_event: Mapping[str, Any]) -> list[dict[str, Any]]:
        """把一个原始 v3 事件展开为一个或多个浏览器协议事件。"""
        if raw_event.get("type") != "event":
            raise ValueError("Deep Agents v3 event must have type='event'")
        raw_sequence_value = raw_event.get("seq")
        if raw_sequence_value is None:
            raw_sequence = self._last_raw_sequence + 1
        elif isinstance(raw_sequence_value, bool) or not isinstance(
            raw_sequence_value, int
        ):
            raise ValueError("protocol event seq must be an integer")
        else:
            raw_sequence = raw_sequence_value
        if raw_sequence <= self._last_raw_sequence:
            raise ValueError("protocol event sequence must be strictly increasing")
        self._last_raw_sequence = raw_sequence
        params = raw_event.get("params") or {}
        if not isinstance(params, Mapping):
            raise ValueError("protocol event params must be an object")
        method_value = raw_event.get("method")
        if method_value is not None and not isinstance(method_value, str):
            raise ValueError("protocol event method must be a string")
        method = method_value or "custom"
        namespace = _normalize_namespace(params.get("namespace", []))
        timestamp = params.get("timestamp")
        data = params.get("data")

        if method != "messages":
            if method == "lifecycle" and isinstance(data, Mapping):
                child_namespace = data.get("namespace")
                if not namespace and child_namespace is not None:
                    namespace = _normalize_namespace(child_namespace)
            flushed = []
            if method == "lifecycle" and isinstance(data, Mapping):
                lifecycle_value = data.get("event") or data.get("status") or ""
                lifecycle_event = (
                    lifecycle_value if isinstance(lifecycle_value, str) else ""
                )
                if lifecycle_event in _TERMINAL_LIFECYCLE_EVENTS:
                    flushed = self._finish_text_streams(
                        timestamp=timestamp,
                        namespace=namespace,
                        source_sequence=raw_sequence,
                    )
            return [
                *flushed,
                self._event(
                    method=method,
                    namespace=namespace,
                    timestamp=timestamp,
                    data=data,
                    source_sequence=raw_sequence,
                ),
            ]

        payload = data
        metadata: Mapping[str, Any] = {}
        if isinstance(data, (list, tuple)) and len(data) == 2:
            payload = data[0]
            if isinstance(data[1], Mapping):
                metadata = data[1]
        node_value = metadata.get("langgraph_node")
        node = node_value if isinstance(node_value, str) and node_value else None
        if isinstance(payload, Mapping) and payload.get("event"):
            message_events = [payload]
        elif isinstance(payload, AIMessageChunk):
            return self._project_text_chunk(
                payload,
                namespace=namespace,
                timestamp=timestamp,
                node=node,
                source_sequence=raw_sequence,
            )
        elif isinstance(payload, BaseMessage) and not isinstance(
            payload, (AIMessageChunk, ToolMessage)
        ):
            message_events = list(message_to_events(payload, message_id=payload.id))
        else:
            return []
        return [
            self._event(
                method="messages",
                namespace=namespace,
                timestamp=timestamp,
                data=message_event,
                node=node,
                source_sequence=raw_sequence,
            )
            for message_event in message_events
        ]

    def _project_text_chunk(
        self,
        chunk: AIMessageChunk,
        *,
        namespace: Any,
        timestamp: Any,
        node: str | None,
        source_sequence: int,
    ) -> list[dict[str, Any]]:
        """把模型文本 chunk 立即投影为浏览器可消费的增量消息事件。"""
        normalized_namespace = tuple(str(item) for item in (namespace or []))
        stream_hint = node or (str(chunk.id or "") if not normalized_namespace else "")
        key = (normalized_namespace, stream_hint)
        state = self._text_streams.get(key)
        text = chunk.text
        events: list[dict[str, Any]] = []

        if text:
            if state is None:
                state = {
                    "id": str(
                        self._root_message_id
                        if not normalized_namespace and self._root_message_id
                        else chunk.id or uuid4()
                    ),
                    "text": "",
                    "namespace": list(normalized_namespace),
                    "node": node,
                }
                self._text_streams[key] = state
                events.extend(
                    [
                        self._event(
                            method="messages",
                            namespace=namespace,
                            timestamp=timestamp,
                            node=node,
                            source_sequence=source_sequence,
                            data={
                                "event": "message-start",
                                "role": "ai",
                                "id": state["id"],
                            },
                        ),
                        self._event(
                            method="messages",
                            namespace=namespace,
                            timestamp=timestamp,
                            node=node,
                            source_sequence=source_sequence,
                            data={
                                "event": "content-block-start",
                                "index": 0,
                                "content": {"type": "text", "text": ""},
                            },
                        ),
                    ]
                )
            state["text"] += text
            events.append(
                self._event(
                    method="messages",
                    namespace=namespace,
                    timestamp=timestamp,
                    node=node,
                    source_sequence=source_sequence,
                    data={
                        "event": "content-block-delta",
                        "index": 0,
                        "delta": {"type": "text-delta", "text": text},
                    },
                )
            )

        terminal = (
            chunk.chunk_position == "last"
            or bool((chunk.response_metadata or {}).get("finish_reason"))
            or bool((chunk.response_metadata or {}).get("stop_reason"))
        )
        if terminal and key in self._text_streams:
            events.extend(
                self._finish_text_stream(
                    key,
                    timestamp=timestamp,
                    source_sequence=source_sequence,
                )
            )
        return events

    def _finish_text_stream(
        self,
        key: tuple[tuple[str, ...], str],
        *,
        timestamp: Any,
        source_sequence: int | None = None,
        source: str = "deepagents",
    ) -> list[dict[str, Any]]:
        state = self._text_streams.pop(key, None)
        if state is None:
            return []
        return [
            self._event(
                method="messages",
                namespace=state["namespace"],
                timestamp=timestamp,
                node=state["node"],
                source_sequence=source_sequence,
                source=source,
                data={
                    "event": "content-block-finish",
                    "index": 0,
                    "content": {"type": "text", "text": state["text"]},
                },
            ),
            self._event(
                method="messages",
                namespace=state["namespace"],
                timestamp=timestamp,
                node=state["node"],
                source_sequence=source_sequence,
                source=source,
                data={"event": "message-finish"},
            ),
        ]

    def _finish_text_streams(
        self,
        *,
        timestamp: Any,
        namespace: tuple[str, ...] | None = None,
        source_sequence: int | None = None,
        source: str = "deepagents",
    ) -> list[dict[str, Any]]:
        """关闭指定 namespace 子树中的文本流；根路径关闭全部。"""
        events: list[dict[str, Any]] = []
        for key in list(self._text_streams):
            stream_namespace = key[0]
            if (
                namespace is not None
                and stream_namespace[: len(namespace)] != namespace
            ):
                continue
            events.extend(
                self._finish_text_stream(
                    key,
                    timestamp=timestamp,
                    source_sequence=source_sequence,
                    source=source,
                )
            )
        return events

__all__ = ["AgentProtocolEventProjector"]
