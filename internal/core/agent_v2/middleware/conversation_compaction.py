"""DeepAgents 0.6 主动压缩适配；阈值、摘要与状态更新仍由原生实现负责。"""

import asyncio

from deepagents.middleware.summarization import SummarizationToolMiddleware, create_summarization_middleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command
from internal.core.agent_v2.persistence.conversation_summary import validate_summary_text
from internal.extension.logging_extension import logger


class ConversationCompactionMiddleware(SummarizationToolMiddleware):
    """补齐当前原生主动工具缺少的媒体卸载，并拒绝同批重复压缩写状态。"""

    def __init__(self, model, backend):
        """与 create_deep_agent 的自动压缩使用同一模型预算及原生默认策略。"""
        super().__init__(create_summarization_middleware(model, backend), system_prompt=None)

    def _run_compact(self, runtime):
        """仅适配原生工具边界；不复制摘要算法、截断算法或消息配对逻辑。"""
        messages = runtime.state.get("messages", [])
        last_ai = next((item for item in reversed(messages) if isinstance(item, AIMessage)), None)
        calls = [item for item in (last_ai.tool_calls if last_ai else []) if item["name"] == "compact_conversation"]
        if len(calls) > 1:
            return Command(update={"messages": [ToolMessage(
                content="同一批次只能调用一次 compact_conversation，请单独调用。",
                tool_call_id=runtime.tool_call_id, status="error",
            )]})
        engine = self._summarization
        event = runtime.state.get("_summarization_event")
        effective = engine._apply_event_to_messages(messages, event)
        if not self._is_eligible_for_compaction(effective):
            return self._nothing_to_compact(runtime.tool_call_id)
        cutoff = engine._determine_cutoff_index(effective)
        if not cutoff:
            return self._nothing_to_compact(runtime.tool_call_id)
        try:
            removed, _ = engine._partition_messages(effective, cutoff)
            backend = self._resolve_backend(runtime)
            removed, failed_media = engine._offload_inline_media(backend, removed)
            summary = engine._create_summary(removed)
            validate_summary_text(summary)
            path = engine._offload_to_backend(backend, removed)
            if failed_media:
                summary += "\n部分媒体未能归档，需要时请用户重新提供，不能声称已读取这些媒体。"
            result = self._build_compact_result(runtime, removed, summary, path, event, cutoff)
            # 仅用于说明主动来源；不覆盖框架的状态结构和 cutoff 算法。
            result.update["_summarization_event"]["platform_reason"] = "manual"
            result.update["_summarization_event"]["platform_media_complete"] = not bool(failed_media)
            return result
        except Exception:
            # 避免原生错误回执把供应商异常与凭证信息原样带入公开工具结果。
            logger.exception("主动上下文压缩失败")
            return Command(update={"messages": [ToolMessage(
                content="上下文压缩失败，现有会话状态保留，请稍后重试。",
                tool_call_id=runtime.tool_call_id, status="error",
            )]})

    async def _arun_compact(self, runtime):
        """共享同步适配和 PostgreSQL 事务，传播当前图上下文到工作线程。"""
        return await asyncio.to_thread(self._run_compact, runtime)
