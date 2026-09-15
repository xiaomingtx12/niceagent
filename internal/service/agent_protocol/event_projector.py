"""将 Core 规范化事件绑定到工作台 Run，不解析框架消息或维护文本流。"""

from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID
import time

from pydantic import ValidationError

from internal.core.agent_v2.entity.agent_event import AgentEvent
from internal.core.agent_v2.entity.stream_event import CustomEventType, EventChannel
from internal.schema.agent.agent_protocol_schema import PUBLIC_EVENT_METHODS, AgentProtocolEvent
from internal.schema.agent.agent_context_schema import ContextCompactionResp, ContextWindowResp
from internal.schema.agent.agent_model_progress_schema import AgentModelProgressResp
from internal.schema.agent.agent_memory_schema import AgentMemoryUpdateResp
from .state_projector import adapt_graph_state

_CUSTOM_SCHEMAS = {
    CustomEventType.CONTEXT_WINDOW: ContextWindowResp,
    CustomEventType.CONTEXT_COMPACTION: ContextCompactionResp,
    CustomEventType.MODEL_PROGRESS: AgentModelProgressResp,
    CustomEventType.MEMORY_FILE_UPDATE: AgentMemoryUpdateResp,
}


class AgentProtocolEventProjector:
    """集中处理工作台可见范围、业务身份与编号；不管理写线程和订阅。"""

    def __init__(self, run_id: UUID | str, *, start_sequence: int = 0) -> None:
        """公开序号可从历史游标继续，Core 执行序号每次恢复重新开始。"""
        if start_sequence < 0:
            raise ValueError("protocol start sequence must be non-negative")
        self.run_id = str(run_id)
        self.last_event_sequence = start_sequence

    def project_agent_event(self, event: AgentEvent, *, turn_id: UUID) -> AgentProtocolEvent | None:
        """过滤发生在编号之前；根图终态由结果保存事务另行发布。"""
        if event.is_root_terminal:
            return None
        data = event.data
        match event.channel:
            case EventChannel.CUSTOM:
                data = normalize_custom_data(data)
                if data is None:
                    return None
            case EventChannel.VALUES:
                data = adapt_graph_state(data)
            case EventChannel.MESSAGES:
                message = event.message
                assert message is not None
                if message.is_tool_argument_delta:
                    return None
                if not event.namespace and message.is_assistant_start:
                    data = {**data, "id": f"{turn_id}:assistant"}
        return self.create_event(
            event.channel, data, namespace=event.namespace, timestamp=event.timestamp,
            node=event.node, source="agent_v2", source_sequence=event.sequence,
            model_run_id=event.model_run_id,
        )

    def create_business_event(
        self, method: Literal["values", "lifecycle", "input.requested"], data: Mapping[str, Any],
        *, namespace: list[str] | None = None,
    ) -> AgentProtocolEvent:
        """业务事务产生的事件与 Core 事件共用持久化序号；来源标识 ``workbench`` 是已发布的线协议取值，不随 Python 命名调整。"""
        return self.create_event(method, data, namespace=namespace or (), source="workbench")

    def create_state_transition(self, state: Mapping[str, Any], lifecycle: Mapping[str, Any]) -> list[AgentProtocolEvent]:
        """状态、表单、生命周期按顺序生成，调用方负责同一事务提交。"""
        # 1.先交付状态，使客户端拿到生成表单所需的快照。
        events = [self.create_business_event(EventChannel.VALUES, state)]
        # 2.每个待输入中断生成独立表单事件，沿用中断 ID 提交答案。
        for item in state.get("pending_inputs", []):
            events.append(self.create_business_event(EventChannel.INPUT_REQUESTED, {
                "interrupt_id": item["interrupt_id"], "payload": item["payload"],
            }))
        # 3.生命周期放在最后，避免客户端先结束订阅而漏掉状态或表单。
        events.append(self.create_business_event(EventChannel.LIFECYCLE, lifecycle))
        return events

    @staticmethod
    def parse_stored_event(event: Mapping[str, Any]) -> AgentProtocolEvent | None:
        """历史读取当前协议外壳，custom 只接受当前保存的 name/payload 结构。"""
        # 1.未知通道不参与重放；普通公开事件直接按协议 DTO 读取。
        method = event.get("method")
        if not isinstance(method, str) or method not in PUBLIC_EVENT_METHODS:
            return None
        if method != EventChannel.CUSTOM:
            return AgentProtocolEvent.model_validate(event)

        # 2.历史 custom 载荷仍须通过当前白名单，不直接信任存储中的任意自定义数据。
        params = event.get("params")
        if not isinstance(params, Mapping):
            return None
        envelope = params.get("data")
        if not isinstance(envelope, Mapping):
            return None
        data = normalize_custom_data(envelope.get("payload"))
        if data is None or envelope.get("name") != data["name"]:
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
    """将 Core 的原始进度载荷校验后包装为当前工作台 name/payload 结构。"""
    # 1.Core 固定提供带 type 的载荷，已包装事件只由历史读取入口解包。
    if not isinstance(data, Mapping):
        return None
    # 2.只有明确登记的自定义类型可以公开，其他类型直接忽略。
    schema = _CUSTOM_SCHEMAS.get(data.get("type"))
    if schema is None:
        return None
    # 3.校验字段后重新序列化，实时交付和历史重放使用相同数据形状。
    try:
        validated = schema.model_validate(data)
    except ValidationError:
        return None
    return {"name": validated.type, "payload": validated.model_dump(mode="json")}
