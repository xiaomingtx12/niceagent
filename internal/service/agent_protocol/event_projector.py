"""将 Core 规范化事件绑定到工作台 Run，不解析框架消息或维护文本流。"""

from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID
import time

from pydantic import ValidationError

from internal.core.agent_v2 import AgentEvent
from internal.schema.agent.agent_workbench_schema import PUBLIC_EVENT_METHODS, WorkbenchEvent
from internal.schema.agent.agent_context_schema import ContextCompactionResp, ContextWindowResp
from .state_projector import project_public_state

_TERMINAL_EVENTS = frozenset({"completed", "failed", "interrupted", "drained", "cancelled"})
_CUSTOM_SCHEMAS = {"context_window": ContextWindowResp, "context_compaction": ContextCompactionResp}


class AgentProtocolEventProjector:
    """只维护持久化事件序号和业务消息标识；一个 Core 事件最多生成一个公开事件。"""

    def __init__(self, run_id: UUID | str, *, start_sequence: int = 0, root_message_id: str | None = None) -> None:
        """公开序号可从历史游标继续，Core 执行序号每次恢复重新开始。"""
        if start_sequence < 0:
            raise ValueError("protocol start sequence must be non-negative")
        self.run_id = str(run_id)
        self.last_event_sequence = start_sequence
        self._root_message_id = root_message_id

    def project(self, event: AgentEvent) -> WorkbenchEvent | None:
        """筛选展示通道并绑定业务身份，根终态由业务事务提交后生成。"""
        # 1.根终态只由平台事务发布，避免图结束早于业务提交。
        data = event.data
        if event.channel == "lifecycle" and not event.namespace:
            if (data.get("event") or data.get("status")) in _TERMINAL_EVENTS:
                return None
        if event.channel == "custom":
            # 2.自定义观测数据必须经过协议白名单校验。
            data = _public_custom_data(data)
            if data is None:
                return None
        elif event.channel == "values":
            data = project_public_state(data)
        elif event.channel == "messages" and not event.namespace and self._root_message_id:
            if data.get("event") == "message-start" and data.get("role") in {"ai", "assistant"}:
                data = {**data, "id": self._root_message_id}
        return self._create_event(
            event.channel, data, namespace=event.namespace, timestamp=event.timestamp,
            node=event.node, source="agent_v2", source_sequence=event.sequence,
        )

    def generated(
        self, method: Literal["values", "lifecycle", "input.requested"], data: Mapping[str, Any],
        *, namespace: list[str] | None = None,
    ) -> WorkbenchEvent:
        """业务事务产生的事件与 Core 事件共用持久化序号。"""
        return self._create_event(method, data, namespace=namespace or (), source="workbench")

    def generated_state_transition(self, state: Mapping[str, Any], lifecycle: Mapping[str, Any]) -> list[WorkbenchEvent]:
        """状态、表单、生命周期按顺序生成，调用方负责同一事务提交。"""
        # 1.先保存状态，再发送表单，最后发送生命周期。
        events = [self.generated("values", state)]
        for item in state.get("pending_inputs", []):
            events.append(self.generated("input.requested", {
                "interrupt_id": item["interrupt_id"], "payload": item["payload"],
            }))
        events.append(self.generated("lifecycle", lifecycle))
        return events

    @staticmethod
    def to_public_event(event: Mapping[str, Any]) -> WorkbenchEvent | None:
        """只用于历史 JSON 读取，实时事件不再走历史兼容转换。"""
        method = event.get("method")
        if not isinstance(method, str) or method not in PUBLIC_EVENT_METHODS:
            return None
        if method == "custom":
            params = event.get("params")
            data = _public_custom_data(params.get("data")) if isinstance(params, Mapping) else None
            if data is None:
                return None
            event = {**event, "params": {**params, "data": data}}
        return WorkbenchEvent.model_validate(event)

    def _create_event(
        self, method: str, data: Mapping[str, Any], *, namespace: tuple[str, ...] | list[str],
        source: str, timestamp: int | float | None = None, node: str | None = None,
        source_sequence: int | None = None,
    ) -> WorkbenchEvent:
        """直接构造最终协议对象，避免字典外壳与 JSON 序列化往返。"""
        self.last_event_sequence += 1
        metadata: dict[str, Any] = {"source": source}
        if source_sequence is not None:
            metadata["source_seq"] = source_sequence
        return WorkbenchEvent(
            type="event", event_id=f"{self.run_id}:{self.last_event_sequence}",
            seq=self.last_event_sequence, method=method,
            params={
                "namespace": list(namespace),
                "timestamp": timestamp if timestamp is not None else int(time.time() * 1000),
                "node": node, "data": data, "metadata": {"my_dify": metadata},
            },
        )


def _public_custom_data(data: Any) -> dict[str, Any] | None:
    """只展示已定义的上下文观测数据，兼容历史命名载荷。"""
    if not isinstance(data, Mapping):
        return None
    payload = data.get("payload") if data.get("name") in _CUSTOM_SCHEMAS else data
    if not isinstance(payload, Mapping):
        return None
    schema = _CUSTOM_SCHEMAS.get(payload.get("type"))
    if schema is None:
        return None
    try:
        validated = schema.model_validate(payload)
    except ValidationError:
        return None
    return {"name": validated.type, "payload": validated.model_dump(mode="json")}
