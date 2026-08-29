"""Agent 模型调用的 Token 用量值对象。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


def _add_details(target: dict[str, int], source: Mapping[str, int]) -> None:
    """把一组 Token 明细累加到目标字典。"""
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def _detail_attribute_name(value: str) -> str:
    """生成稳定的遥测明细字段名称。"""
    name = (
        "".join(
            character if character.isalnum() else "_" for character in value.lower()
        ).strip("_")
        or "other"
    )
    return name if name.endswith("_tokens") else f"{name}_tokens"


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
        _add_details(self.input_details, other.input_details)
        _add_details(self.output_details, other.output_details)
        if self.source == "unknown":
            self.source = other.source
        self.present = self.present or other.present

    def to_attributes(self) -> dict[str, int | str]:
        """把用量及明细展开为遥测标量属性。"""
        total = self.total_tokens or self.input_tokens + self.output_tokens
        attributes: dict[str, int | str] = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": total,
            "usage_source": self.source,
        }
        attributes.update(
            {
                f"input_{_detail_attribute_name(key)}": value
                for key, value in self.input_details.items()
            }
        )
        attributes.update(
            {
                f"output_{_detail_attribute_name(key)}": value
                for key, value in self.output_details.items()
            }
        )
        return attributes

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化的字典。"""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens or self.input_tokens + self.output_tokens,
            "input_details": dict(self.input_details),
            "output_details": dict(self.output_details),
            "source": self.source,
            "present": self.present,
        }


@dataclass
class AgentTokenUsageSummary:
    """一次 Run 按模型聚合后的 Token 用量。"""

    by_model: dict[str, AgentTokenUsage] = field(default_factory=dict)
    total: AgentTokenUsage = field(default_factory=AgentTokenUsage)
    call_count: int = 0

    def add(
        self,
        usage: AgentTokenUsage,
        *,
        model_name: str | None = None,
    ) -> None:
        """把一次模型调用加入当前 Run。"""
        if not usage.present:
            return
        model_key = model_name or "unknown"
        model_usage = self.by_model.setdefault(model_key, AgentTokenUsage())
        model_usage.add(usage)
        self.total.add(usage)
        self.call_count += 1

    @property
    def present(self) -> bool:
        """判断是否至少记录了一条模型用量。"""
        return self.total.present

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化的 Run 用量结构。"""
        return {
            "total": self.total.to_dict(),
            "by_model": {
                model_name: usage.to_dict()
                for model_name, usage in self.by_model.items()
            },
            "call_count": self.call_count,
            "present": self.present,
        }


__all__ = ["AgentTokenUsage", "AgentTokenUsageSummary"]
