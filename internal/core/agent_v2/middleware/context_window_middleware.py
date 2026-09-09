"""在最终模型请求边界观察上下文组成，不公开提示词、记忆正文或工具参数。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from langchain.agents.middleware.types import AgentMiddleware, AgentState, ModelRequest
from langchain_core.messages import BaseMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.stream import CustomTransformer

from internal.core.agent_v2.entity.memory_entry import estimate_memory_tokens
from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.execution.message_parser import message_content_to_text, response_messages


class ContextWindowMiddleware(AgentMiddleware[AgentState, AgentRunContext, Any]):
    """最后一个平台模型包装器观察压缩、技能和记忆装配后的请求。"""

    # v3 只订阅已注册 transformer 的通道，stream_writer 本身不会开启 custom。
    transformers = (CustomTransformer,)

    def wrap_model_call(self, request: ModelRequest, handler):
        """调用开始提供估算，返回后用本次供应商用量补充真实输入数量。"""
        snapshot = build_context_snapshot(request)
        request.runtime.stream_writer(snapshot)
        try:
            response = handler(request)
        except Exception:
            request.runtime.stream_writer({**snapshot, "status": "failed"})
            raise
        request.runtime.stream_writer(complete_context_snapshot(snapshot, response))
        return response

    async def awrap_model_call(self, request: ModelRequest, handler):
        """异步链路使用同一请求快照，不将多个模型调用的 token 累加成窗口占用。"""
        snapshot = build_context_snapshot(request)
        request.runtime.stream_writer(snapshot)
        try:
            response = await handler(request)
        except Exception:
            request.runtime.stream_writer({**snapshot, "status": "failed"})
            raise
        request.runtime.stream_writer(complete_context_snapshot(snapshot, response))
        return response


def build_context_snapshot(request: ModelRequest) -> dict[str, Any]:
    """输出固定分类的计数；未知窗口容量或输出上限保持 null，不猜测模型配置。"""
    system = message_content_to_text(request.system_message.content) if request.system_message else ""
    memory_blocks = re.findall(r"<长期记忆(?:规则|参考)>.*?</长期记忆(?:规则|参考)>", system, re.DOTALL)
    memory = "\n".join(memory_blocks)
    for block in memory_blocks:
        system = system.replace(block, "", 1)
    sections = {
        "system": {"key": "system", "label": "系统与技能", "tokens": estimate_memory_tokens(system), "count": int(bool(system))},
        "memory": {"key": "memory", "label": "长期记忆与规则", "tokens": estimate_memory_tokens(memory), "count": len(memory_blocks)},
        "human": {"key": "human", "label": "用户消息", "tokens": 0, "count": 0},
        "assistant": {"key": "assistant", "label": "助手消息与工具调用", "tokens": 0, "count": 0},
        "tool": {"key": "tool", "label": "工具返回", "tokens": 0, "count": 0},
        "tools": {"key": "tools", "label": "工具定义", "tokens": 0, "count": len(request.tools or [])},
    }
    multimodal = False
    for message in request.messages:
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
        # 不按 base64 字符长度伪造媒体 token；供应商返回时才有可用的实际输入总量。
        text = message_content_to_text(content)
        if calls:
            text += json.dumps(calls, ensure_ascii=False, default=str)
        sections[key]["tokens"] += estimate_memory_tokens(text) + 4
        sections[key]["count"] += 1
    tools = [dict(item) if isinstance(item, Mapping) else convert_to_openai_tool(item) for item in request.tools or []]
    sections["tools"]["tokens"] = estimate_memory_tokens(json.dumps(tools, ensure_ascii=False)) if tools else 0
    metadata = getattr(request.model, "metadata", None) or {}
    profile = getattr(request.model, "profile", None) or {}
    window = positive_int(metadata.get("context_window"))
    input_limit = positive_int(metadata.get("max_input_tokens")) or positive_int(profile.get("max_input_tokens"))
    settings = request.model_settings or {}
    reserved = next((value for value in (
        positive_int(settings.get("max_completion_tokens")), positive_int(settings.get("max_tokens")),
        positive_int(metadata.get("configured_output_tokens")),
        positive_int(getattr(request.model, "max_tokens", None)),
    ) if value is not None), None)
    return {
        "type": "context_window", "call_id": str(uuid4()), "status": "running",
        "model": str(getattr(request.model, "model_name", None) or getattr(request.model, "model", None) or request.model.__class__.__name__),
        "context_limit": window or input_limit, "limit_kind": "context" if window else "input",
        "output_reserve": reserved, "estimated_input_tokens": sum(section["tokens"] for section in sections.values()),
        "actual_input_tokens": None, "sections": list(sections.values()), "has_multimodal": multimodal,
    }


def complete_context_snapshot(snapshot: dict, response: Any) -> dict:
    """供应商未提供输入用量时保留估算，不用本轮累计用量替代单次请求。"""
    actual = None
    for message in response_messages(response):
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
                break
        if actual is not None:
            break
    return {**snapshot, "status": "completed", "actual_input_tokens": actual}


def positive_int(value: Any) -> int | None:
    """排除 bool、零和未知配置，避免展示虚假的窗口容量。"""
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
