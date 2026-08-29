"""兼容不同模型 Provider 的 Token 用量归一化和聚合。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage, AIMessageChunk

from internal.core.agent_v2.protocol.message_parser import response_messages
from internal.core.agent_v2.entity.token_usage import (
    AgentTokenUsage,
    AgentTokenUsageSummary,
)


_INPUT_KEYS = ("input_tokens", "prompt_tokens", "prompt_token_count")
_OUTPUT_KEYS = ("output_tokens", "completion_tokens", "completion_token_count")
_TOTAL_KEYS = ("total_tokens", "total_token_count")


def normalize_message(message: Any) -> AgentTokenUsage:
    """优先读取标准字段，并兼容 Provider 扩展字段。"""
    if not isinstance(message, (AIMessage, AIMessageChunk)):
        return AgentTokenUsage()

    usage_metadata = getattr(message, "usage_metadata", None)
    if isinstance(usage_metadata, Mapping):
        usage = normalize_usage(usage_metadata, source="usage_metadata")
        if usage.present:
            return usage

    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, Mapping):
        for key in ("token_usage", "usage", "usage_metadata"):
            usage = normalize_usage(
                response_metadata.get(key),
                source=f"response_metadata.{key}",
            )
            if usage.present:
                return usage

    additional_kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(additional_kwargs, Mapping):
        for key in ("token_usage", "usage", "usage_metadata"):
            usage = normalize_usage(
                additional_kwargs.get(key),
                source=f"additional_kwargs.{key}",
            )
            if usage.present:
                return usage

    return AgentTokenUsage()


def normalize_response(response: Any) -> AgentTokenUsage:
    """归一化模型响应中的全部 AI 消息。"""
    usage = AgentTokenUsage()
    for message in response_messages(response):
        current = normalize_message(message)
        if current.present:
            usage.add(current)
            if usage.source == "unknown":
                usage.source = current.source
    return usage


def normalize_usage(raw: Any, *, source: str = "unknown") -> AgentTokenUsage:
    """归一化标准格式或 Provider 自定义格式的 Token 用量映射。"""
    if not isinstance(raw, Mapping):
        return AgentTokenUsage()

    input_tokens = _first_int(raw, _INPUT_KEYS)
    output_tokens = _first_int(raw, _OUTPUT_KEYS)
    total_tokens = _first_int(raw, _TOTAL_KEYS)
    input_details = _details(raw, "input")
    output_details = _details(raw, "output")
    present = bool(
        input_tokens
        or output_tokens
        or total_tokens
        or input_details
        or output_details
        or any(key in raw for key in (*_INPUT_KEYS, *_OUTPUT_KEYS, *_TOTAL_KEYS))
    )
    return AgentTokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens or input_tokens + output_tokens,
        input_details=input_details,
        output_details=output_details,
        source=source,
        present=present,
    )


def build_framework_run_usage(
    framework_usage: Mapping[str, Mapping[str, Any]] | None,
    *,
    usage_id: UUID | str,
    trace_id: UUID | str,
    tool_call_count: int = 0,
) -> dict[str, Any] | None:
    """把 LangChain 回调用量转换为统一的 Run 级持久化结构。"""
    aggregate = AgentTokenUsageSummary()
    if framework_usage:
        for model_name, raw_usage in framework_usage.items():
            aggregate.add(
                normalize_usage(raw_usage, source="langchain_callback"),
                model_name=str(model_name),
            )
    if not aggregate.present and not tool_call_count:
        return None

    total = aggregate.total
    return {
        "trace_id": str(trace_id),
        "usage_id": str(usage_id),
        "model_count": len(aggregate.by_model),
        "model_call_count": aggregate.call_count,
        "tool_call_count": tool_call_count,
        "usage_source": "langchain_callback",
        "usage_present": aggregate.present,
        "input_tokens": total.input_tokens,
        "output_tokens": total.output_tokens,
        "total_tokens": total.total_tokens or total.input_tokens + total.output_tokens,
        "input_details": dict(total.input_details),
        "output_details": dict(total.output_details),
        "models": {
            model_name: usage.to_dict()
            for model_name, usage in aggregate.by_model.items()
        },
    }


def _first_int(raw: Mapping[str, Any], keys: Iterable[str]) -> int:
    """按候选字段顺序读取第一个有效整数。"""
    for key in keys:
        value = raw.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def _details(raw: Mapping[str, Any], direction: str) -> dict[str, int]:
    """提取指定方向的缓存、推理等 Token 明细。"""
    details: dict[str, int] = {}
    detail_keys = (
        f"{direction}_token_details",
        f"{direction}_tokens_details",
        f"{'prompt' if direction == 'input' else 'completion'}_tokens_details",
    )
    for detail_key in detail_keys:
        raw_details = raw.get(detail_key)
        if isinstance(raw_details, Mapping):
            for key, value in raw_details.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    details[str(key)] = int(value)

    aliases = (
        ("cached", "cached_tokens"),
        ("cache_read", "cache_read_input_tokens"),
        ("cache_creation", "cache_creation_input_tokens"),
        ("reasoning", "reasoning_tokens"),
    )
    for detail_name, raw_key in aliases:
        value = raw.get(raw_key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            details.setdefault(detail_name, int(value))
    return details


__all__ = [
    "normalize_message",
    "normalize_response",
    "normalize_usage",
    "build_framework_run_usage",
]
