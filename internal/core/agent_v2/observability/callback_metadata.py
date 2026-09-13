"""仅解析统计所需模型名称和摘要进度所需异常状态。"""

import asyncio
from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from langchain_core.outputs import LLMResult
from langgraph.errors import GraphInterrupt


def bounded_text(value: Any, *, default: str = "unknown") -> str:
    """模型标识有界保存，不读取输入输出正文。"""
    if not isinstance(value, (str, int, float, bool, UUID)):
        return default
    text = str(value).strip()
    return text[:128] if text else default


def request_model_name(
    serialized: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
    kwargs: Mapping[str, Any],
) -> str:
    """优先采用请求中的模型名，再回退到框架元数据及序列化名称。"""
    invocation = kwargs.get("invocation_params")
    if isinstance(invocation, Mapping):
        for key in ("model", "model_name", "model_id"):
            value = invocation.get(key)
            if isinstance(value, str) and value:
                return bounded_text(value)
    if isinstance(metadata, Mapping) and metadata.get("ls_model_name"):
        return bounded_text(metadata["ls_model_name"])
    if isinstance(serialized, Mapping):
        if isinstance(serialized.get("name"), str) and serialized["name"]:
            return bounded_text(serialized["name"])
        identifier = serialized.get("id")
        if isinstance(identifier, (list, tuple)) and identifier:
            return bounded_text(identifier[-1])
    return "unknown"


def response_model_name(response: LLMResult) -> str | None:
    """返回实际模型名以替换请求别名，不读取回复正文。"""
    for generations in response.generations:
        for generation in generations:
            message = getattr(generation, "message", None)
            metadata = getattr(message, "response_metadata", None)
            if not isinstance(metadata, Mapping):
                continue
            for key in ("model_name", "model", "model_id"):
                value = metadata.get(key)
                if isinstance(value, str) and value:
                    return bounded_text(value)
    if isinstance(response.llm_output, Mapping):
        for key in ("model_name", "model", "model_id"):
            value = response.llm_output.get(key)
            if isinstance(value, str) and value:
                return bounded_text(value)
    return None


def exception_status(error: BaseException) -> Literal["waiting_input", "cancelled", "error"]:
    """摘要通知区分框架中断、协作取消和调用失败。"""
    if isinstance(error, GraphInterrupt):
        return "waiting_input"
    if isinstance(error, (asyncio.CancelledError, GeneratorExit)):
        return "cancelled"
    return "error"
