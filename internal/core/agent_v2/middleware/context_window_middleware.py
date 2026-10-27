"""观察平台装配后的请求组成；估算不等同于供应商最终序列化输入。"""

from __future__ import annotations

import json
import hashlib
import re
import time
from collections.abc import Mapping
from math import ceil
from typing import Any
from uuid import uuid4

from langchain.agents.middleware.types import AgentMiddleware, AgentState, ModelRequest
from langchain_core.messages import BaseMessage
from langchain_core.messages.utils import count_tokens_approximately
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.stream import CustomTransformer

from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.execution.message_text import extract_text
from internal.core.agent_v2.assembly.configuration.context_budget import positive_int, request_input_budget
from internal.core.agent_v2.observability.token_usage import normalize_usage
from internal.core.agent_v2.persistence.conversation_summary import validate_summary_message
from internal.core.language_model.entities.model_entity import DEFAULT_CONTEXT_WINDOW


class ContextWindowMiddleware(AgentMiddleware[AgentState, AgentRunContext, Any]):
    """观察有效消息和固定规则，后续供应商适配仍可能调整请求表示。"""

    # 每个图只在此注册一次 custom；记忆、模型进度与压缩通知共用此通道。
    # 框架直接拼接中间件的 transformers，重复声明会在流初始化时冲突。
    transformers = (CustomTransformer,)

    def wrap_model_call(self, request: ModelRequest, handler):
        """调用开始提供估算，返回后用本次供应商用量补充真实输入数量。"""
        snapshot = build_context_snapshot(request)
        started = time.monotonic()
        request.runtime.stream_writer(snapshot)
        try:
            response = handler(request)
        except Exception:
            request.runtime.stream_writer({**snapshot, "status": "failed"})
            raise
        request.runtime.stream_writer({**complete_context_snapshot(snapshot, response), "duration_ms": round((time.monotonic() - started) * 1000)})
        return response

    async def awrap_model_call(self, request: ModelRequest, handler):
        """异步链路使用同一请求快照，不将多个模型调用的 token 累加成窗口占用。"""
        snapshot = build_context_snapshot(request)
        started = time.monotonic()
        request.runtime.stream_writer(snapshot)
        try:
            response = await handler(request)
        except Exception:
            request.runtime.stream_writer({**snapshot, "status": "failed"})
            raise
        request.runtime.stream_writer({**complete_context_snapshot(snapshot, response), "duration_ms": round((time.monotonic() - started) * 1000)})
        return response


def build_context_snapshot(request: ModelRequest) -> dict[str, Any]:
    """窗口缺省采用平台默认值并标明来源；输出预留仍按实际请求配置读取。"""
    system = extract_text(request.system_message.content) if request.system_message else ""
    memory_blocks = re.findall(r"<长期记忆规则>.*?</长期记忆规则>", system, re.DOTALL)
    memory = "\n".join(memory_blocks)
    for block in memory_blocks:
        system = system.replace(block, "", 1)
    sections = {
        "system": {"key": "system", "label": "系统与技能", "tokens": _estimate_text_tokens(system), "count": int(bool(system))},
        "memory": {"key": "memory", "label": "长期记忆使用规则", "tokens": _estimate_text_tokens(memory), "count": len(memory_blocks)},
        "human": {"key": "human", "label": "用户消息", "tokens": 0, "count": 0},
        "assistant": {"key": "assistant", "label": "助手消息与工具调用", "tokens": 0, "count": 0},
        "tool": {"key": "tool", "label": "工具返回", "tokens": 0, "count": 0},
        "tools": {"key": "tools", "label": "工具定义", "tokens": 0, "count": len(request.tools or [])},
        "summary": {"key": "summary", "label": "当前历史摘要", "tokens": 0, "count": 0},
    }
    multimodal = False
    for message in request.messages:
        validate_summary_message(message)
        if isinstance(message, BaseMessage):
            role, content = message.type, message.content
            calls = getattr(message, "tool_calls", [])
        elif isinstance(message, Mapping):
            role, content = message.get("type", message.get("role")), message.get("content", "")
            calls = message.get("tool_calls", [])
        else:
            continue
        if isinstance(content, list) and any(isinstance(block, Mapping) and block.get("type") not in {"text", None} for block in content):
            multimodal = True
        key = "human" if role in {"human", "user"} else "tool" if role == "tool" else "system" if role == "system" else "assistant"
        extra = message.additional_kwargs if isinstance(message, BaseMessage) else message.get("additional_kwargs", {})
        if extra.get("lc_source") == "summarization":
            key = "summary"
        # 不按 base64 字符长度伪造媒体 token；供应商返回时才有可用的实际输入总量。
        text = extract_text(content)
        if calls:
            text += json.dumps(calls, ensure_ascii=False, default=str)
        sections[key]["tokens"] += _estimate_text_tokens(text) + 4
        sections[key]["count"] += 1
    tools = [dict(item) if isinstance(item, Mapping) else convert_to_openai_tool(item) for item in request.tools or []]
    sections["tools"]["tokens"] = _estimate_text_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0
    metadata = getattr(request.model, "metadata", None) or {}
    profile = getattr(request.model, "profile", None) or {}
    window = positive_int(metadata.get("context_window"))
    default_window = window is None or metadata.get("context_window_source") == "default"
    window = window or DEFAULT_CONTEXT_WINDOW
    input_limit = positive_int(metadata.get("max_input_tokens")) if "max_input_tokens" in metadata else positive_int(profile.get("max_input_tokens"))
    settings = request.model_settings or {}
    reserved = next((value for value in (
        positive_int(settings.get("max_completion_tokens")), positive_int(settings.get("max_tokens")),
        positive_int(metadata.get("configured_output_tokens")),
        positive_int(getattr(request.model, "max_tokens", None)),
    ) if value is not None), None)
    budget = request_input_budget(request.model)
    counted = ([request.system_message] if request.system_message else []) + list(request.messages)
    compaction_count = count_tokens_approximately(counted, tools=request.tools)
    fixed_count = count_tokens_approximately([request.system_message] if request.system_message else [], tools=request.tools)
    if budget and fixed_count >= budget:
        raise ValueError("系统提示与工具定义已用尽模型输入预算，请减少固定配置或提高可用窗口")
    return {
        "type": "context_window", "call_id": str(uuid4()), "status": "running",
        "model": str(getattr(request.model, "model_name", None) or getattr(request.model, "model", None) or request.model.__class__.__name__),
        "context_limit": window, "limit_kind": "context",
        "context_limit_is_default": default_window,
        "max_input_tokens": input_limit,
        "output_reserve": reserved, "estimated_input_tokens": sum(section["tokens"] for section in sections.values()),
        "actual_input_tokens": None, "sections": list(sections.values()), "has_multimodal": multimodal,
        "input_budget": budget, "safety_tokens": positive_int(metadata.get("context_safety_tokens")),
        "compaction_trigger_tokens": int(budget * 0.85) if budget else 170000,
        "compaction_input_tokens": compaction_count,
        "system_fingerprint": hashlib.sha256((system + memory).encode()).hexdigest(),
        "tools_fingerprint": hashlib.sha256(json.dumps(tools, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
    }


def complete_context_snapshot(snapshot: dict, response: Any) -> dict:
    """供应商未提供输入用量时保留估算，不用本轮累计用量替代单次请求。"""
    actual = None
    cached = created = ratio = None
    for message in _response_messages(response):
        raw_usage = getattr(message, "usage_metadata", None)
        response_metadata = getattr(message, "response_metadata", {}) or {}
        additional = getattr(message, "additional_kwargs", {}) or {}
        candidates = [raw_usage, *[container.get(key) for container in (response_metadata, additional) for key in ("token_usage", "usage", "usage_metadata")]]
        for candidate in candidates:
            if not isinstance(candidate, Mapping):
                continue
            for key in ("input_tokens", "prompt_tokens", "prompt_token_count"):
                value = candidate.get(key)
                if type(value) is int and value >= 0:
                    actual = value
                    break
                if isinstance(value, str) and value.isascii() and value.isdecimal():
                    actual = int(value)
                    break
            if actual is not None:
                details = normalize_usage(candidate).input_details
                cached, created = details.get("cached"), details.get("cache_creation")
                # Anthropic 原始 input_tokens 不含缓存；标准 usage_metadata 已完成归一化。
                if candidate is not raw_usage and "cache_read_input_tokens" in candidate and "input_tokens" in candidate:
                    actual += (cached or 0) + (created or 0)
                known_total = candidate is raw_usage or "prompt_tokens" in candidate or "cache_read_input_tokens" in candidate
                ratio = cached / actual if known_total and cached is not None and actual and cached <= actual else None
                break
        if actual is not None:
            break
    return {**snapshot, "status": "completed", "actual_input_tokens": actual,
            "cache_read_tokens": cached, "cache_creation_tokens": created, "cache_read_ratio": ratio}


def _response_messages(response: Any) -> list[BaseMessage]:
    """为本中间件读取供应商用量拆开模型响应包装，不改变原响应对象。"""
    model_response = getattr(response, "model_response", response)
    result = getattr(model_response, "result", model_response)
    if isinstance(result, BaseMessage):
        return [result]
    if isinstance(result, list):
        return [message for message in result if isinstance(message, BaseMessage)]
    return []


def _estimate_text_tokens(value: str) -> int:
    """窗口展示使用保守的 UTF-8 字节估算，不代表供应商实际计费用量。"""
    return ceil(len(value.encode("utf-8")) / 2)
