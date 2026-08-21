"""兼容不同模型 Provider 的 Token 用量归一化和聚合。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage


_INPUT_KEYS = ("input_tokens", "prompt_tokens", "prompt_token_count")
_OUTPUT_KEYS = ("output_tokens", "completion_tokens", "completion_token_count")
_TOTAL_KEYS = ("total_tokens", "total_token_count")


@dataclass
class TokenUsage:
    """一次模型响应或回调记录归一化后的 Token 用量。"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    input_details: dict[str, int] = field(default_factory=dict)
    output_details: dict[str, int] = field(default_factory=dict)
    source: str = "unknown"
    present: bool = False

    def add(self, other: "TokenUsage") -> None:
        """累加另一条归一化后的用量记录。"""
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.total_tokens += other.total_tokens
        _add_details(self.input_details, other.input_details)
        _add_details(self.output_details, other.output_details)
        if self.source == "unknown":
            self.source = other.source
        self.present = self.present or other.present

    def to_attributes(self) -> dict[str, int | str]:
        """把用量及明细展开为遥测所需的标量属性。"""
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
        """把用量转换为可持久化和传输的字典。"""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens
            or self.input_tokens + self.output_tokens,
            "input_details": dict(self.input_details),
            "output_details": dict(self.output_details),
            "source": self.source,
            "present": self.present,
        }


@dataclass
class TokenUsageAccumulator:
    """按模型聚合用量，同时保留所有模型的总用量。"""

    by_model: dict[str, TokenUsage] = field(default_factory=dict)
    total: TokenUsage = field(default_factory=TokenUsage)
    call_count: int = 0

    def add(self, usage: TokenUsage, *, model_name: str | None = None) -> None:
        """把一次模型调用加入当前运行聚合结果。"""
        if not usage.present:
            return
        model_key = model_name or "unknown"
        model_usage = self.by_model.setdefault(model_key, TokenUsage())
        model_usage.add(usage)
        self.total.add(usage)
        self.call_count += 1

    def add_mapping(
        self,
        usage_by_model: Mapping[str, Mapping[str, Any]],
        *,
        source: str,
    ) -> None:
        """加入按模型名称组织的 LangChain 回调结果。"""
        for model_name, raw_usage in usage_by_model.items():
            usage = normalize_usage(raw_usage, source=source)
            self.add(usage, model_name=str(model_name))

    @property
    def present(self) -> bool:
        """判断是否至少有一条模型响应提供了用量。"""
        return self.total.present

    def to_dict(self) -> dict[str, Any]:
        """把运行级用量转换为前端和持久化都能使用的结构。"""
        return {
            "total": self.total.to_dict(),
            "by_model": {
                model_name: usage.to_dict()
                for model_name, usage in self.by_model.items()
            },
            "call_count": self.call_count,
            "present": self.present,
        }


def normalize_message(message: Any) -> TokenUsage:
    """优先读取标准字段，并兼容 Provider 扩展字段。"""
    if not isinstance(message, (AIMessage, AIMessageChunk)):
        return TokenUsage()

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

    return TokenUsage()


def normalize_response(response: Any, *, model_name: str | None = None) -> TokenUsage:
    """归一化模型响应中的全部 AI 消息。"""
    usage = TokenUsage()
    for message in response_messages(response):
        current = normalize_message(message)
        if current.present:
            usage.add(current)
            if usage.source == "unknown":
                usage.source = current.source
    return usage


def normalize_usage(raw: Any, *, source: str = "unknown") -> TokenUsage:
    """归一化标准格式或 Provider 自定义格式的 Token 用量映射。"""
    if not isinstance(raw, Mapping):
        return TokenUsage()

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
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens or input_tokens + output_tokens,
        input_details=input_details,
        output_details=output_details,
        source=source,
        present=present,
    )


def response_messages(response: Any) -> list[BaseMessage]:
    """从类似 LangChain ModelResponse 的包装对象中提取消息。"""
    model_response = getattr(response, "model_response", response)
    result = getattr(model_response, "result", model_response)
    if isinstance(result, BaseMessage):
        return [result]
    if isinstance(result, list):
        return [message for message in result if isinstance(message, BaseMessage)]
    return []


def _first_int(raw: Mapping[str, Any], keys: Iterable[str]) -> int:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def _details(raw: Mapping[str, Any], direction: str) -> dict[str, int]:
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


def _add_details(target: dict[str, int], source: Mapping[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + value


def _safe_detail_name(value: str) -> str:
    return "".join(
        character if character.isalnum() else "_"
        for character in value.lower()
    ).strip("_") or "other"


def _detail_attribute_name(value: str) -> str:
    """生成稳定的明细后缀，避免重复添加 ``_tokens``。"""
    name = _safe_detail_name(value)
    return name if name.endswith("_tokens") else f"{name}_tokens"


__all__ = [
    "TokenUsage",
    "TokenUsageAccumulator",
    "normalize_message",
    "normalize_response",
    "normalize_usage",
]
