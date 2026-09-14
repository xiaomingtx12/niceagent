"""平台业务 Message 到工作台历史消息的投影。"""

from collections.abc import Mapping, Sequence
from uuid import UUID
from typing import Any

from internal.entity import AGENT_RUN_ACTIVE_STATUSES

from internal.model import AgentRun, Message
from internal.schema.agent.agent_protocol_schema import AgentProtocolHistoryMessageResp


def project_conversation_messages(
    rows: Sequence[Message],
    *,
    runs_by_message: Mapping[UUID, AgentRun] | None = None,
) -> list[AgentProtocolHistoryMessageResp]:
    """仅在协议适配层把一个业务回合拆成 human 和 ai 消息。"""
    messages: list[AgentProtocolHistoryMessageResp] = []
    for row in rows:
        run = (runs_by_message or {}).get(row.id)
        run_id = str(run.id) if run is not None else row.trace_id
        messages.append(
            AgentProtocolHistoryMessageResp(
                id=(
                    run.input_message_id
                    if run is not None and run.input_message_id
                    else f"{row.id}:human"
                ),
                type="human",
                content=row.query,
                additional_kwargs={
                    "business_message_id": str(row.id),
                    "run_id": run_id,
                    "image_urls": list(row.image_urls or []),
                    "attachments": list(row.attachments or []),
                },
            )
        )
        if row.answer:
            messages.append(
                AgentProtocolHistoryMessageResp(
                    id=f"{row.id}:assistant",
                    type="ai",
                    content=row.answer,
                    additional_kwargs={
                        "run_id": run_id,
                        "metrics": {
                            "latency": float(row.latency or 0),
                            "total_token_count": int(row.total_token_count or 0),
                            "input_token_count": int(row.input_token_count or 0),
                            "output_token_count": int(row.output_token_count or 0),
                            "cached_token_count": int(row.cached_token_count or 0),
                            "reasoning_token_count": int(
                                row.reasoning_token_count or 0
                            ),
                        },
                    },
                )
            )
    return messages


def merge_running_messages(
    messages: list[dict[str, Any]],
    run: AgentRun | None,
    snapshot: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """活动 Run 的快照补齐尚未落库的消息，只允许覆盖本轮的消息标识。"""
    if run is None or run.status not in AGENT_RUN_ACTIVE_STATUSES:
        return messages
    current_ids = {
        str(run.input_message_id),
        f"{run.message_id}:human",
        f"{run.message_id}:assistant",
    }
    by_id = {message["id"]: message for message in messages}
    for message in snapshot.get("messages", []):
        if isinstance(message, dict) and message.get("id") in current_ids:
            by_id[message["id"]] = message
    return list(by_id.values())


__all__ = ["project_conversation_messages", "merge_running_messages"]
