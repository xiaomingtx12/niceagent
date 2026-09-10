"""平台业务 Message 到工作台历史消息的投影。"""

from collections.abc import Mapping, Sequence
from uuid import UUID

from internal.model import AgentRun, Message
from internal.schema.agent.agent_workbench_schema import WorkbenchHistoryMessageResp


def project_conversation_messages(
    rows: Sequence[Message],
    *,
    runs_by_message: Mapping[UUID, AgentRun] | None = None,
) -> list[WorkbenchHistoryMessageResp]:
    """仅在协议适配层把一个业务回合拆成 human 和 ai 消息。"""
    messages: list[WorkbenchHistoryMessageResp] = []
    for row in rows:
        run = (runs_by_message or {}).get(row.id)
        run_id = str(run.id) if run is not None else row.trace_id
        messages.append(
            WorkbenchHistoryMessageResp(
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
                WorkbenchHistoryMessageResp(
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


__all__ = ["project_conversation_messages"]
