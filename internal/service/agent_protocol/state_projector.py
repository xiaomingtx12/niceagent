"""执行事件与状态查询共用的图状态适配和公开快照筛选。"""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

_PUBLIC_STATE_KEYS = ("todos", "files", "async_tasks", "pending_inputs", "input_history", "input_recovery")


def adapt_graph_state(value: Mapping[str, Any]) -> dict[str, Any]:
    """适配图状态的消息和会话字段，保留其余字段；公开快照另由白名单提取。"""
    # 1.创建独立状态，后续字段改写不能影响 Core 事件或已保存的原始快照。
    projected = deepcopy(dict(value))

    # 2.快照每轮只保留最后一条 AI 回答；完整阶段正文仍由 messages 事件重放。
    messages = projected.get("messages")
    if isinstance(messages, list):
        visible: list[dict[str, Any]] = []
        business_message_id = None
        turn_run_id = None
        assistant_index = None
        for message in messages:
            if not isinstance(message, dict):
                continue

            # 用户消息开始新回合，同时重置回答位置，避免跨回合合并。
            role = message.get("type") or message.get("role")
            if role in {"human", "user"}:
                extra = message.get("additional_kwargs") or {}
                business_message_id = extra.get("business_message_id")
                turn_run_id = extra.get("run_id")
                assistant_index = None
                visible.append(message)
                continue

            # 工具、系统及没有业务归属的消息保留原样。
            if role not in {"ai", "assistant"} or not business_message_id:
                visible.append(message)
                continue

            # 本轮 AI 回答共用业务 ID，更新正文但保留第一次回答的位置。
            message["id"] = f"{business_message_id}:assistant"
            message.setdefault("additional_kwargs", {})["run_id"] = turn_run_id
            if assistant_index is not None:
                visible[assistant_index] = message
                continue
            assistant_index = len(visible)
            visible.append(message)
        projected["messages"] = visible

    # 3.异步任务沿用平台 conversation_id 命名，不覆盖已有的业务会话标识。
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


def select_public_snapshot(snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """只公开会话状态，过滤模型调用配置和内部表单恢复信息。"""
    source = snapshot or {}
    return {key: source[key] for key in _PUBLIC_STATE_KEYS if key in source}
