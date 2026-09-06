"""将 Deep Agents v3 原始事件投影为 Agent Protocol 传输结构。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any, NamedTuple
from uuid import UUID, uuid4

from langchain_core.language_models._compat_bridge import message_to_events
from langchain_core.messages import AIMessageChunk, BaseMessage, ToolMessage

from internal.schema.agent_workbench_schema import WorkbenchEvent
from .event_sanitizer import project_workbench_event

_TERMINAL_LIFECYCLE_EVENTS = frozenset(
    {"completed", "failed", "interrupted", "drained", "cancelled"}
)
_SKILL_TOOL_NAME = "read_file"
_SKILL_FILE_NAME = "SKILL.md"


class _SkillRef(NamedTuple):
    """一次文件工具调用对应的 Skill 路径。"""

    name: str
    path: str


class _ActiveSkillCall(NamedTuple):
    """正在读取 Skill 文件的工具调用。"""

    ref: _SkillRef
    report: bool


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
        return {str(key): _json_value(item) for key, item in value.items()}
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


def _project_public_state(value: Mapping[str, Any]) -> dict[str, Any]:
    """把框架状态字段转换为平台公开的会话语义。"""
    projected = _json_value(value)
    if not isinstance(projected, dict):
        return {}
    async_tasks = projected.get("async_tasks")
    if not isinstance(async_tasks, dict):
        return projected
    for task in async_tasks.values():
        if not isinstance(task, dict):
            continue
        framework_thread_id = task.pop("thread_id", None)
        if framework_thread_id is not None:
            task.setdefault("conversation_id", framework_thread_id)
    return projected


class AgentProtocolEventProjector:
    """把一次 DeepAgents v3 运行转换成有序的 Agent Protocol 事件流。

    本类只负责事件编号、消息分片合并和协议形状，不负责数据库、Redis
    或 SSE；公开通道投影由 ``event_sanitizer`` 统一完成。
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
        # Skill 是通过原生 read_file 工具按需加载的；按路径去重，避免模型
        # 重试或分页重复读取时让工作台重复展示同一个 Skill。
        self._used_skill_paths: set[tuple[tuple[str, ...], str]] = set()
        self._active_skill_calls: dict[
            tuple[tuple[str, ...], str], _ActiveSkillCall
        ] = {}

    @staticmethod
    def to_public_event(
        event: Mapping[str, Any] | WorkbenchEvent,
    ) -> WorkbenchEvent | None:
        """完成 Agent Protocol 公开通道投影和结构校验。"""
        raw_event = _json_value(
            event.model_dump(mode="python")
            if isinstance(event, WorkbenchEvent)
            else event
        )
        visible = project_workbench_event(raw_event)
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

    def finish_pending_skill_events(
        self,
        *,
        status: str,
        reason: str | None = None,
    ) -> list[dict[str, Any]]:
        """在 Run 终态补齐仍未结束的 Skill 读取事件。"""
        events: list[dict[str, Any]] = []
        for call_key, active in list(self._active_skill_calls.items()):
            self._active_skill_calls.pop(call_key, None)
            if not active.report:
                continue
            data: dict[str, Any] = {
                "event": "skill_finished",
                "skill_name": active.ref.name,
                "skill_path": active.ref.path,
                "status": status,
                "tool_call_id": call_key[1],
            }
            if reason:
                data["reason"] = reason
            events.append(
                self._event(
                    method="lifecycle",
                    namespace=list(call_key[0]),
                    timestamp=None,
                    data=data,
                    source="workbench",
                )
            )
        return events

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
            projected_event_data = data
            if method == "values" and isinstance(data, Mapping):
                projected_event_data = _project_public_state(data)
            skill_events: list[dict[str, Any]] = []
            if method == "tools" and isinstance(data, Mapping):
                projected_event_data, skill_events = self._project_skill_tool_event(
                    data,
                    namespace=namespace,
                    timestamp=timestamp,
                    source_sequence=raw_sequence,
                )
            return [
                *flushed,
                *skill_events,
                self._event(
                    method=method,
                    namespace=namespace,
                    timestamp=timestamp,
                    data=projected_event_data,
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

    def _project_skill_tool_event(
        self,
        data: Mapping[str, Any],
        *,
        namespace: tuple[str, ...],
        timestamp: Any,
        source_sequence: int,
    ) -> tuple[Mapping[str, Any], list[dict[str, Any]]]:
        """从原生文件工具事件派生 Skill 生命周期，不参与 Agent 执行。"""
        event_type = data.get("event")
        call_id_value = data.get("tool_call_id")
        call_id = str(call_id_value) if call_id_value is not None else ""
        call_key = (namespace, call_id)
        active = self._active_skill_calls.get(call_key)
        ref = (
            _skill_ref_from_tool_event(data)
            if event_type == "tool-started"
            else active.ref if active is not None else None
        )

        # Skill 生命周期是工作台派生事件；原始工具事件及其正文保持不变。
        projected_data: Mapping[str, Any] = data

        derived: list[dict[str, Any]] = []
        if event_type == "tool-started" and ref is not None and call_id:
            path_key = (namespace, ref.path)
            report = path_key not in self._used_skill_paths and not any(
                item.ref.path == ref.path and item.ref.name == ref.name
                for item in self._active_skill_calls.values()
            )
            self._active_skill_calls[call_key] = _ActiveSkillCall(ref, report)
            if report:
                derived.append(
                    self._event(
                        method="lifecycle",
                        namespace=namespace,
                        timestamp=timestamp,
                        data={
                            "event": "skill_started",
                            "skill_name": ref.name,
                            "skill_path": ref.path,
                            "status": "running",
                            "tool_call_id": call_id,
                        },
                        source_sequence=source_sequence,
                        source="workbench",
                    )
                )
        elif event_type in {"tool-finished", "tool-error"} and active is not None:
            self._active_skill_calls.pop(call_key, None)
            if event_type == "tool-finished":
                self._used_skill_paths.add((namespace, active.ref.path))
            if active.report:
                if event_type == "tool-finished":
                    status = "completed"
                    event_name = "skill_finished"
                else:
                    status = "failed"
                    event_name = "skill_error"
                derived.append(
                    self._event(
                        method="lifecycle",
                        namespace=namespace,
                        timestamp=timestamp,
                        data={
                            "event": event_name,
                            "skill_name": active.ref.name,
                            "skill_path": active.ref.path,
                            "status": status,
                            "tool_call_id": call_id,
                        },
                        source_sequence=source_sequence,
                        source="workbench",
                    )
                )

        return projected_data, derived

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


def _skill_ref_from_tool_event(data: Mapping[str, Any]) -> _SkillRef | None:
    """从 read_file 参数识别主 Agent 或 SubAgent 的 Skill 包路径。"""
    tool_name = data.get("tool_name")
    if tool_name != _SKILL_TOOL_NAME:
        return None
    inputs = data.get("input")
    if not isinstance(inputs, Mapping):
        return None
    raw_path = inputs.get("file_path")
    if not isinstance(raw_path, str) or not raw_path.startswith("/"):
        return None
    # Backend 路径是 POSIX 虚拟路径；拒绝反斜杠、相对段和非规范路径，
    # 防止把用户可控的任意文件路径误判成 Skill。
    try:
        normalized_path = str(PurePosixPath(raw_path))
    except (TypeError, ValueError):
        return None
    if raw_path != normalized_path or "\\" in raw_path:
        return None
    parts = PurePosixPath(raw_path).parts
    if len(parts) == 4 and parts[0] == "/" and parts[1] == "skills":
        skill_name = parts[2]
        if skill_name and parts[-1] == _SKILL_FILE_NAME:
            return _SkillRef(skill_name, raw_path)
    if (
        len(parts) == 5
        and parts[0] == "/"
        and parts[1] == "subagent_skills"
        and parts[-1] == _SKILL_FILE_NAME
    ):
        skill_name = parts[-2]
        if skill_name:
            return _SkillRef(skill_name, raw_path)
    return None


__all__ = ["AgentProtocolEventProjector"]
