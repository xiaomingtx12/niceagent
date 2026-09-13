"""将运行结果和持久化快照转换为公开状态，不访问数据库。"""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from internal.core.agent_v2 import (
    AgentExecutionResult,
    PendingUserInput,
)
from internal.entity import WorkbenchRunStatus
from copy import deepcopy

_PUBLIC_STATE_KEYS = ("todos", "files", "async_tasks", "pending_inputs", "input_history", "input_recovery")


def project_public_state(value: Mapping[str, Any]) -> dict[str, Any]:
    """绑定业务消息标识和异步会话字段；输入已由 Core 转为 JSON 兼容数据。"""
    projected = deepcopy(dict(value))
    # 会话摘要保留最后一条回答；阶段正文由公开 messages 事件独立重放。
    messages = projected.get("messages")
    if isinstance(messages, list):
        visible = []
        business_id = None
        run_id = None
        answer_index = None
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = message.get("type") or message.get("role")
            if role in {"human", "user"}:
                extra = message.get("additional_kwargs") or {}
                business_id = extra.get("business_message_id")
                run_id = extra.get("run_id")
                answer_index = None
                visible.append(message)
            elif role in {"ai", "assistant"} and business_id:
                message["id"] = f"{business_id}:assistant"
                message.setdefault("additional_kwargs", {})["run_id"] = run_id
                if answer_index is None:
                    answer_index = len(visible)
                    visible.append(message)
                else:
                    visible[answer_index] = message
            else:
                visible.append(message)
        projected["messages"] = visible
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


def project_execution_state(
    result: AgentExecutionResult, previous_snapshot: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """只公开执行结果的展示状态和已校验表单，答案历史沿用平台已提交快照。"""
    state = project_public_state({
        key: result.state[key] for key in ("todos", "files", "async_tasks")
        if key in result.state
    })
    state.update(
        pending_inputs=[
            PendingUserInput.model_validate(item).model_dump(mode="json")
            for item in result.interrupts
        ],
        input_history=list((previous_snapshot or {}).get("input_history", [])),
        input_recovery=None,
    )
    return state


def project_execution_lifecycle(
    status: WorkbenchRunStatus,
    *,
    message_id: UUID,
    input_message_id: str | None,
    public_error: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """将执行结束状态映射为实时生命周期；业务层仅传入可公开的错误信息。"""
    data: dict[str, Any] = {
        "event": status.value,
        "status": status.value,
        "message_id": str(message_id),
        "input_message_id": input_message_id,
    }
    if status is WorkbenchRunStatus.WAITING_INPUT:
        data.update(event="interrupted", reason="user_input_required")
    elif status is WorkbenchRunStatus.CANCELLED:
        data.update(event="interrupted", reason="cancel_requested")
    elif status is WorkbenchRunStatus.FAILED:
        data["error"] = public_error or "Agent 运行失败"
        if error_code is not None:
            data["code"] = error_code
    return data


def project_snapshot_values(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """只公开会话状态，过滤模型调用配置和内部表单恢复信息。"""
    source = snapshot or {}
    return {key: source[key] for key in _PUBLIC_STATE_KEYS if key in source}


def project_pending_tasks(
    status: str,
    values: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """把待输入快照转换为工作台任务，保持客户端表单协议结构。"""
    if status != "waiting_input":
        return []

    tasks = []
    for item in values.get("pending_inputs", []):
        interrupt_id = item["interrupt_id"]
        interrupt = {
            "id": interrupt_id,
            "value": item["payload"],
            "ns": item.get("namespace", []),
        }
        tasks.append(
            {
                "id": interrupt_id,
                "name": "ask_user",
                "interrupts": [interrupt],
            }
        )
    return tasks
