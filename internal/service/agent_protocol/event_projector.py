"""将 Core 规范化事件绑定到工作台 Run，不解析框架消息或维护文本流。"""

from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID
import time

from pydantic import ValidationError

from internal.schema.agent.agent_protocol_schema import PUBLIC_EVENT_METHODS, AgentProtocolEvent
from internal.schema.agent.agent_context_schema import ContextCompactionResp, ContextWindowResp
from internal.schema.agent.agent_model_progress_schema import AgentModelProgressResp

_CUSTOM_SCHEMAS = {
    "context_window": ContextWindowResp,
    "context_compaction": ContextCompactionResp,
    "model_progress": AgentModelProgressResp,
}
TOOL_ARGUMENT_CONTENT_TYPES = ("tool_call", "tool_call_chunk", "server_tool_call", "server_tool_call_chunk")


def is_tool_argument_delta(data: Mapping[str, Any]) -> bool:
    """只过滤已识别的参数生成分片；完整调用、正文和未知内容仍交付工作台。"""
    if data.get("event") != "content-block-delta":
        return False
    content = data.get("content")
    if isinstance(content, Mapping):
        return content.get("type") in TOOL_ARGUMENT_CONTENT_TYPES
    delta = data.get("delta")
    if not isinstance(delta, Mapping) or delta.get("type") != "block-delta":
        return False
    fields = delta.get("fields")
    return isinstance(fields, Mapping) and fields.get("type") in TOOL_ARGUMENT_CONTENT_TYPES


class AgentProtocolEventProjector:
    """共用 Run 的持久化事件序号；生成协议外壳并解析历史事件。"""

    def __init__(self, run_id: UUID | str, *, start_sequence: int = 0) -> None:
        """公开序号可从历史游标继续，Core 执行序号每次恢复重新开始。"""
        if start_sequence < 0:
            raise ValueError("protocol start sequence must be non-negative")
        self.run_id = str(run_id)
        self.last_event_sequence = start_sequence

    def create_business_event(
        self, method: Literal["values", "lifecycle", "input.requested"], data: Mapping[str, Any],
        *, namespace: list[str] | None = None,
    ) -> AgentProtocolEvent:
        """业务事务产生的事件与 Core 事件共用持久化序号；来源标识 ``workbench`` 是已发布的线协议取值，不随 Python 命名调整。"""
        return self.create_event(method, data, namespace=namespace or (), source="workbench")

    def create_state_transition(self, state: Mapping[str, Any], lifecycle: Mapping[str, Any]) -> list[AgentProtocolEvent]:
        """状态、表单、生命周期按顺序生成，调用方负责同一事务提交。"""
        # 1.先交付状态，使客户端拿到生成表单所需的快照。
        events = [self.create_business_event("values", state)]
        # 2.每个待输入中断生成独立表单事件，沿用中断 ID 提交答案。
        for item in state.get("pending_inputs", []):
            events.append(self.create_business_event("input.requested", {
                "interrupt_id": item["interrupt_id"], "payload": item["payload"],
            }))
        # 3.生命周期放在最后，避免客户端先结束订阅而漏掉状态或表单。
        events.append(self.create_business_event("lifecycle", lifecycle))
        return events

    @staticmethod
    def parse_stored_event(event: Mapping[str, Any]) -> AgentProtocolEvent | None:
        """只用于历史 JSON 读取，实时事件不再走历史兼容转换。"""
        # 1.未知通道不参与重放；普通公开事件直接按协议 DTO 读取。
        method = event.get("method")
        if not isinstance(method, str) or method not in PUBLIC_EVENT_METHODS:
            return None
        if method != "custom":
            return AgentProtocolEvent.model_validate(event)

        # 2.历史 custom 载荷仍须通过当前白名单，不直接信任存储中的任意自定义数据。
        params = event.get("params")
        if not isinstance(params, Mapping):
            return None
        data = normalize_custom_data(params.get("data"))
        if data is None:
            return None

        # 3.仅替换规范化后的载荷，保留历史事件 ID、序号和其他参数。
        return AgentProtocolEvent.model_validate({**event, "params": {**params, "data": data}})

    def create_event(
        self, method: str, data: Mapping[str, Any], *, namespace: tuple[str, ...] | list[str],
        source: str, timestamp: int | float | None = None, node: str | None = None,
        source_sequence: int | None = None,
        model_run_id: str | None = None,
    ) -> AgentProtocolEvent:
        """直接构造最终协议对象，避免字典外壳与 JSON 序列化往返。"""
        # 1.框架事件与业务事件共用平台序号，事件 ID 由 Run ID 和该序号组成。
        self.last_event_sequence += 1
        # 2.来源序号和模型调用 ID 放入 metadata，业务身份仍由当前 Run 确定。
        metadata: dict[str, Any] = {"source": source}
        if source_sequence is not None:
            metadata["source_seq"] = source_sequence
        if model_run_id is not None:
            metadata["model_run_id"] = model_run_id
        # 3.优先保留事件原时间，只有缺少时间时才补当前时间。
        return AgentProtocolEvent(
            type="event", event_id=f"{self.run_id}:{self.last_event_sequence}",
            seq=self.last_event_sequence, method=method,
            params={
                "namespace": list(namespace),
                "timestamp": timestamp if timestamp is not None else int(time.time() * 1000),
                "node": node, "data": data, "metadata": {"my_dify": metadata},
            },
        )


def normalize_custom_data(data: Any) -> dict[str, Any] | None:
    """只展示已定义的公开进度数据，兼容历史命名载荷。"""
    # 1.兼容原始 payload 与已包装的历史载荷，统一取出待校验数据。
    if not isinstance(data, Mapping):
        return None
    payload = data.get("payload") if data.get("name") in _CUSTOM_SCHEMAS else data
    if not isinstance(payload, Mapping):
        return None
    # 2.只有明确登记的自定义类型可以公开，其他类型直接忽略。
    schema = _CUSTOM_SCHEMAS.get(payload.get("type"))
    if schema is None:
        return None
    # 3.校验字段后重新序列化，实时交付和历史重放使用相同数据形状。
    try:
        validated = schema.model_validate(payload)
    except ValidationError:
        return None
    return {"name": validated.type, "payload": validated.model_dump(mode="json")}
