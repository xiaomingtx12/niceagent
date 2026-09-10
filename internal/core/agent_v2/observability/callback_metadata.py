"""从框架回调提取脱敏调用标识，集中处理模型与工具元数据差异。"""

import asyncio
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from langchain_core.outputs import LLMResult
from langgraph.errors import GraphInterrupt

from internal.core.agent_v2.entity.telemetry import TelemetryValue

_MODEL_METADATA_KEYS = (
    "langgraph_node",
    "lc_agent_name",
    "ls_model_name",
    "ls_model_type",
    "ls_provider",
    "configured_reasoning_effort",
    "lc_source",
)
_TOOL_METADATA_KEYS = (
    "langgraph_node",
    "lc_agent_name",
)


def _bounded_text(value: Any, *, default: str = "unknown") -> str:
    """把观测标识转换为有界文本，避免业务载荷进入 Trace。"""
    if not isinstance(value, (str, int, float, bool, UUID)):
        return default
    text = str(value).strip()
    return text[:128] if text else default


def _safe_attributes(
    values: Mapping[str, Any] | None,
    *,
    allowed_keys: tuple[str, ...] | None = None,
) -> dict[str, TelemetryValue]:
    """只保留有界标量属性。"""
    if not isinstance(values, Mapping):
        return {}
    keys = allowed_keys or tuple(str(key) for key in values)
    attributes: dict[str, TelemetryValue] = {}
    for key in keys:
        value = values.get(key)
        if isinstance(value, bool):
            attributes[str(key)] = value
        elif isinstance(value, (int, float)):
            attributes[str(key)] = value
        elif isinstance(value, str) and value:
            attributes[str(key)] = value[:256]
    return attributes


def _serialized_name(serialized: Mapping[str, Any] | None) -> str:
    """从框架序列化信息中提取运行对象名称。"""
    if not isinstance(serialized, Mapping):
        return "unknown"
    name = serialized.get("name")
    if isinstance(name, str) and name:
        return _bounded_text(name)
    identifier = serialized.get("id")
    if isinstance(identifier, (list, tuple)) and identifier:
        return _bounded_text(identifier[-1])
    return "unknown"


def _model_identity(
    serialized: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
    kwargs: Mapping[str, Any],
) -> tuple[str, dict[str, TelemetryValue]]:
    """从模型开始事件提取名称、Provider 和框架节点。"""
    attributes = _safe_attributes(metadata, allowed_keys=_MODEL_METADATA_KEYS)
    invocation = kwargs.get("invocation_params")
    if isinstance(invocation, Mapping):
        for key in ("model", "model_name", "model_id"):
            value = invocation.get(key)
            if isinstance(value, str) and value:
                attributes.setdefault("model_name", _bounded_text(value))
                break
    model_name = _bounded_text(
        attributes.get("model_name")
        or attributes.get("ls_model_name")
        or _serialized_name(serialized)
    )
    attributes["model_name"] = model_name
    if "model_provider" not in attributes and "ls_provider" in attributes:
        attributes["model_provider"] = attributes["ls_provider"]
    return model_name, attributes


def _response_model_name(response: LLMResult) -> str | None:
    """从模型结果元数据读取实际模型名，不读取回复正文。"""
    for generations in response.generations:
        for generation in generations:
            message = getattr(generation, "message", None)
            metadata = getattr(message, "response_metadata", None)
            if not isinstance(metadata, Mapping):
                continue
            for key in ("model_name", "model", "model_id"):
                value = metadata.get(key)
                if isinstance(value, str) and value:
                    return _bounded_text(value)
    llm_output = response.llm_output
    if isinstance(llm_output, Mapping):
        for key in ("model_name", "model", "model_id"):
            value = llm_output.get(key)
            if isinstance(value, str) and value:
                return _bounded_text(value)
    return None


def _exception_status(error: BaseException) -> str:
    """区分协作式取消和真正的调用失败。"""
    if isinstance(error, GraphInterrupt):
        return "waiting_input"
    if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
        return "cancelled"
    return "error"


def _async_subagent_attributes(output: Any) -> dict[str, TelemetryValue]:
    """从异步 SubAgent 的 Command 中提取子 Trace 标识，不读取结果正文。"""
    update = getattr(output, "update", None)
    if not isinstance(update, Mapping):
        return {}
    tasks = update.get("async_tasks")
    if not isinstance(tasks, Mapping) or len(tasks) != 1:
        return {}
    task_id, task = next(iter(tasks.items()))
    if not isinstance(task, Mapping):
        return {}
    attributes: dict[str, TelemetryValue] = {
        "async_task_id": _bounded_text(task_id),
    }
    child_run_id = task.get("run_id")
    if isinstance(child_run_id, (str, UUID)):
        attributes["child_trace_id"] = _bounded_text(child_run_id)
    child_conversation_id = task.get("conversation_id", task.get("thread_id"))
    if isinstance(child_conversation_id, (str, UUID)):
        attributes["child_conversation_id"] = _bounded_text(child_conversation_id)
    return attributes
