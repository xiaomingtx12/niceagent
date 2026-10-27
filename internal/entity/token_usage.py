"""Agent 模型调用的 Token 用量值对象。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from typing import Any, TypedDict


class RunUsage(TypedDict):
    """一次执行片段的用量；累计与结算去重由业务事务负责。"""

    trace_id: str
    model_call_count: int
    tool_call_count: int
    usage_present: bool
    input_tokens: int
    output_tokens: int
    total_tokens: int
    input_details: dict[str, int]
    output_details: dict[str, int]
    models: dict[str, dict[str, Any]]


def normalize_token_details(raw: Any) -> dict[str, int]:
    """新响应与历史快照共用别名规则；同一份用量中的别名不重复相加。"""
    if not isinstance(raw, Mapping):
        return {}
    details = {
        str(key): int(value)
        for key, value in raw.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        and isfinite(value) and value >= 0
    }
    aliases = {
        "cached": ("cached", "cached_tokens", "cache_read", "cache_read_tokens", "cache_read_input_tokens"),
        "cache_creation": ("cache_creation", "cache_creation_tokens", "cache_creation_input_tokens"),
        "reasoning": ("reasoning", "reasoning_tokens"),
    }
    for name, keys in aliases.items():
        value = next((details[key] for key in keys if key in details), None)
        for key in keys:
            details.pop(key, None)
        if value is not None:
            details[name] = value
    return details


def _add_details(target: dict[str, int], source: Mapping[str, int]) -> None:
    """把一组 Token 明细累加到目标字典。"""
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def merge_model_token_usage(
    previous: Mapping[str, Any], current: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """按模型合并已记录用量，兼容历史明细别名，只输出 Token 字段。"""
    result = {}
    for name in previous.keys() | current.keys():
        old, new = previous.get(name, {}), current.get(name, {})
        tokens: dict[str, Any] = {
            key: int(old.get(key, 0) or 0) + int(new.get(key, 0) or 0)
            for key in ("input_tokens", "output_tokens", "total_tokens")
        }
        for key in ("input_details", "output_details"):
            details = normalize_token_details(old.get(key))
            _add_details(details, normalize_token_details(new.get(key)))
            tokens[key] = details
        result[name] = tokens
    return result


@dataclass
class AgentTokenUsage:
    """一次模型响应归一化后的 Token 用量。"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    input_details: dict[str, int] = field(default_factory=dict)
    output_details: dict[str, int] = field(default_factory=dict)
    source: str = "unknown"
    present: bool = False

    def add(self, other: "AgentTokenUsage") -> None:
        """累加另一条归一化用量。"""
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.total_tokens += other.total_tokens
        _add_details(self.input_details, normalize_token_details(other.input_details))
        _add_details(self.output_details, normalize_token_details(other.output_details))
        if self.source == "unknown":
            self.source = other.source
        self.present = self.present or other.present


    def to_dict(self) -> dict[str, Any]:
        """只交付用量数值，来源与是否缺失由归一化阶段处理。"""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens or self.input_tokens + self.output_tokens,
            "input_details": dict(self.input_details),
            "output_details": dict(self.output_details),
        }


@dataclass
class AgentTokenUsageSummary:
    """一次执行片段按模型聚合后的 Token 用量。"""

    by_model: dict[str, AgentTokenUsage] = field(default_factory=dict)
    total: AgentTokenUsage = field(default_factory=AgentTokenUsage)

    def add(
        self,
        usage: AgentTokenUsage,
        *,
        model_name: str | None = None,
    ) -> None:
        """把一次模型调用加入当前执行片段。"""
        if not usage.present:
            return
        model_key = model_name or "unknown"
        model_usage = self.by_model.setdefault(model_key, AgentTokenUsage())
        model_usage.add(usage)
        self.total.add(usage)

    @property
    def present(self) -> bool:
        """判断是否至少记录了一条模型用量。"""
        return self.total.present


__all__ = [
    "AgentTokenUsage", "AgentTokenUsageSummary", "RunUsage",
    "merge_model_token_usage", "normalize_token_details",
]
