"""Deep Agents v3 raw events projected to the Agent Protocol wire shape.

The projector deliberately keeps channel, namespace and sequence semantics
intact.  It is the transport boundary for the workbench; legacy QueueEvent
translation remains a separate compatibility path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from langchain_core.language_models._compat_bridge import message_to_events
from langchain_core.messages import AIMessageChunk, BaseMessage, ToolMessage


_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}


def _json_value(value: Any) -> Any:
    """Convert LangChain messages and framework values to JSON-safe data."""
    if isinstance(value, BaseMessage):
        try:
            return value.model_dump(mode="json")
        except Exception:
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
    if hasattr(value, "model_dump"):
        try:
            return _json_value(value.model_dump(mode="json"))
        except Exception:
            pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class ProtocolEventProjector:
    """Add stable run metadata while retaining the v3 event payload."""

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
        self.last_sequence = start_sequence
        self._last_raw_sequence = 0
        self._root_message_id = root_message_id
        self._text_streams: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}

    def _event(
        self,
        *,
        method: str,
        namespace: Any,
        timestamp: Any,
        data: Any,
        node: str | None = None,
    ) -> dict[str, Any]:
        self.last_sequence += 1
        params = {
            "namespace": _json_value(namespace or []),
            "timestamp": timestamp,
            "data": _json_value(data),
        }
        if node:
            params["node"] = node
        return {
            "type": "event",
            "event_id": f"{self.run_id}:{self.last_sequence}",
            "seq": self.last_sequence,
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
        )

    def finish_pending_messages(self) -> list[dict[str, Any]]:
        """在运行结束边界关闭未携带 provider finish 标记的文本流。"""
        return self._finish_text_streams(timestamp=None)

    def project_many(self, raw_event: Mapping[str, Any]) -> list[dict[str, Any]]:
        """把一个 Deep Agents v3 事件展开为浏览器 Agent Protocol 事件。"""
        if raw_event.get("type") != "event":
            raise ValueError("Deep Agents v3 event must have type='event'")
        raw_sequence = int(raw_event.get("seq") or self._last_raw_sequence + 1)
        if raw_sequence <= self._last_raw_sequence:
            raise ValueError("protocol event sequence must be strictly increasing")
        self._last_raw_sequence = raw_sequence
        params = raw_event.get("params") or {}
        if not isinstance(params, Mapping):
            raise ValueError("protocol event params must be an object")
        method = str(raw_event.get("method") or "custom")
        namespace = params.get("namespace", [])
        timestamp = params.get("timestamp")
        data = params.get("data")

        if method != "messages":
            flushed = []
            if method == "lifecycle" and isinstance(data, Mapping):
                lifecycle_event = str(data.get("event") or data.get("status") or "")
                if lifecycle_event in {"completed", "failed", "cancelled"}:
                    flushed = self._finish_text_streams(timestamp=timestamp)
            if method == "lifecycle" and isinstance(data, Mapping):
                child_namespace = data.get("namespace")
                if not namespace and isinstance(child_namespace, list):
                    namespace = child_namespace
            return [*flushed, self._event(
                method=method,
                namespace=namespace,
                timestamp=timestamp,
                data=data,
            )]

        payload = data
        metadata: Mapping[str, Any] = {}
        if isinstance(data, (list, tuple)) and len(data) == 2:
            payload = data[0]
            if isinstance(data[1], Mapping):
                metadata = data[1]
        node = str(metadata.get("langgraph_node") or "") or None
        if isinstance(payload, Mapping) and payload.get("event"):
            message_events = [payload]
        elif isinstance(payload, AIMessageChunk):
            return self._project_text_chunk(
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
        return [
            self._event(
                method="messages",
                namespace=namespace,
                timestamp=timestamp,
                data=message_event,
                node=node,
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
                events.extend([
                    self._event(
                        method="messages",
                        namespace=namespace,
                        timestamp=timestamp,
                        node=node,
                        data={"event": "message-start", "role": "ai", "id": state["id"]},
                    ),
                    self._event(
                        method="messages",
                        namespace=namespace,
                        timestamp=timestamp,
                        node=node,
                        data={
                            "event": "content-block-start",
                            "index": 0,
                            "content": {"type": "text", "text": ""},
                        },
                    ),
                ])
            state["text"] += text
            events.append(self._event(
                method="messages",
                namespace=namespace,
                timestamp=timestamp,
                node=node,
                data={
                    "event": "content-block-delta",
                    "index": 0,
                    "delta": {"type": "text-delta", "text": text},
                },
            ))

        terminal = (
            chunk.chunk_position == "last"
            or bool((chunk.response_metadata or {}).get("finish_reason"))
            or bool((chunk.response_metadata or {}).get("stop_reason"))
        )
        if terminal and key in self._text_streams:
            events.extend(self._finish_text_stream(key, timestamp=timestamp))
        return events

    def _finish_text_stream(
        self,
        key: tuple[tuple[str, ...], str],
        *,
        timestamp: Any,
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
                data={"event": "message-finish"},
            ),
        ]

    def _finish_text_streams(self, *, timestamp: Any) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for key in list(self._text_streams):
            events.extend(self._finish_text_stream(key, timestamp=timestamp))
        return events

    def project(self, raw_event: Mapping[str, Any]) -> dict[str, Any]:
        """兼容仅产生单个 wire event 的调用方。"""
        events = self.project_many(raw_event)
        if len(events) != 1:
            raise ValueError("raw event expands to multiple protocol events")
        return events[0]


__all__ = ["ProtocolEventProjector"]
