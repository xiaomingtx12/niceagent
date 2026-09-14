"""通过框架回调累计分析所需用量，不保存调用链或诊断明细。"""

from __future__ import annotations

from threading import RLock
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from internal.entity.token_usage import AgentTokenUsageSummary, RunUsage
from internal.core.agent_v2.observability.callback_metadata import request_model_name, response_model_name
from internal.core.agent_v2.observability.token_usage import normalize_llm_result


class PlatformAgentRunObserver(BaseCallbackHandler):
    """每次执行独占用量累计器；暂停恢复后的合并和去重由持久化层负责。"""

    raise_error = False
    run_inline = True

    def __init__(self, trace_id: UUID) -> None:
        """只保留尚未结束的调用，用于模型归属、调用计数和重复结束保护。"""
        super().__init__()
        self._trace_id = trace_id
        self._lock = RLock()
        self._finished = False
        self._active_models: dict[UUID, str] = {}
        self._active_tools: set[UUID] = set()
        self._model_call_count = 0
        self._tool_call_count = 0
        self._usage = AgentTokenUsageSummary()

    @property
    def run_id(self) -> UUID:
        """提供平台业务 Run 标识，供消息和框架配置关联。"""
        return self._trace_id

    @property
    def trace_id(self) -> str:
        """用量使用可序列化的稳定 Run 标识。"""
        return str(self._trace_id)

    def finish(self) -> None:
        """冻结统计并释放活动调用；未返回用量的调用仍计入调用总数。"""
        with self._lock:
            # 冻结后所有回调立即短路；活动 ID 释放，调用计数保留。
            self._finished = True
            self._active_models.clear()
            self._active_tools.clear()

    def build_run_usage(self) -> RunUsage:
        """按真实模型累计实测用量，明确区分未返回用量和零用量。"""
        with self._lock:
            total = self._usage.total
            return {
                "trace_id": self.trace_id,
                "model_call_count": self._model_call_count,
                "tool_call_count": self._tool_call_count,
                # 未返回用量（usage_present=False）与实测零用量是两种账目，不互相冒充。
                "usage_present": self._usage.present,
                "input_tokens": total.input_tokens,
                "output_tokens": total.output_tokens,
                "total_tokens": total.total_tokens or total.input_tokens + total.output_tokens,
                "input_details": dict(total.input_details),
                "output_details": dict(total.output_details),
                "models": {
                    name: usage.to_dict() for name, usage in self._usage.by_model.items()
                },
            }

    def on_chat_model_start(
        self, serialized: dict[str, Any], messages: list[list[Any]], *, run_id: UUID,
        metadata: dict[str, Any] | None = None, **kwargs: Any,
    ) -> None:
        """聊天模型只登记调用身份，不保存请求正文或节点属性。"""
        self._start_model(run_id, request_model_name(serialized, metadata, kwargs))

    def on_llm_start(
        self, serialized: dict[str, Any], prompts: list[str], *, run_id: UUID,
        metadata: dict[str, Any] | None = None, **kwargs: Any,
    ) -> None:
        """非聊天模型采用相同的用量统计口径。"""
        self._start_model(run_id, request_model_name(serialized, metadata, kwargs))

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        """消费调用与累计用量在同一把锁内，重复结束不能再次计费。"""
        with self._lock:
            # 1.重复结束或冻结后的迟到回调直接忽略，不重复计费。
            if self._finished or run_id not in self._active_models:
                return
            # 2.归一化用量；响应携带的真实模型名优先于请求时的配置别名。
            usage = normalize_llm_result(response)
            name = response_model_name(response) or self._active_models[run_id]
            # 3.Provider 未返回用量时只结束调用，不写入空账目。
            if usage.present:
                self._usage.add(usage, model_name=name)
            self._active_models.pop(run_id)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """失败或取消的模型计入调用数，但不虚构 Provider 未返回的用量。"""
        with self._lock:
            # 只结束登记；失败调用保留开始时的调用计数，没有可累计的用量。
            if self._finished:
                return
            self._active_models.pop(run_id, None)

    def on_tool_start(
        self, serialized: dict[str, Any], input_str: str, *, run_id: UUID, **kwargs: Any,
    ) -> None:
        """工具调用包含子 Agent 调度；只保留活动 ID 以防重复开始计数。"""
        with self._lock:
            # 重复开始不计数；开始即计数，失败也不回退调用数。
            if self._finished or run_id in self._active_tools:
                return
            self._active_tools.add(run_id)
            self._tool_call_count += 1

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        """成功或错误 ToolMessage 均结束本次调用，不保存工具输出。"""
        with self._lock:
            self._active_tools.discard(run_id)

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        """失败或取消时释放活动 ID，调用数保留开始时的记录。"""
        with self._lock:
            self._active_tools.discard(run_id)

    def _start_model(self, run_id: UUID, name: str) -> None:
        """登记一次真实请求；结束回调可用实际模型名替换配置别名。"""
        with self._lock:
            # 按调用 ID 去重；开始即计数，与用量是否返回无关。
            if self._finished or run_id in self._active_models:
                return
            self._active_models[run_id] = name
            self._model_call_count += 1


__all__ = ["PlatformAgentRunObserver"]
