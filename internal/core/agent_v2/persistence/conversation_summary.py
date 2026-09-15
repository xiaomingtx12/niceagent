"""DeepAgents 0.6 私有摘要状态的唯一查询边界；公开接口不返回 checkpoint。"""

import hashlib
import re
from collections.abc import Mapping

from internal.core.agent_v2.execution.message_text import extract_text


def validate_summary_text(content):
    """当前 LangChain 将摘要异常返回为普通文本；禁止把它当作有效摘要提交。"""
    text = content.strip()
    if not text or text.startswith("Error generating summary:") or text == "Previous conversation was too long to summarize.":
        raise ValueError("上下文摘要生成失败，未替换原有上下文")


def validate_summary_message(message):
    """仅检查带框架摘要来源标记的消息，不按用户正文猜测内部状态。"""
    extra = message.get("additional_kwargs", {}) if isinstance(message, Mapping) else getattr(message, "additional_kwargs", {})
    if extra.get("lc_source") != "summarization":
        return
    content = extract_text(message.get("content", "") if isinstance(message, Mapping) else message.content)
    match = re.search(r"<summary>\n(.*)\n</summary>$", content, re.DOTALL)
    summary = match.group(1) if match else content.removeprefix("Here is a summary of the conversation to date:\n\n")
    validate_summary_text(summary)


def read_conversation_summary(checkpointer, conversation_id):
    """只读取已提交根图 checkpoint，pending writes 与生成草稿不算已保存摘要。"""
    if not checkpointer:
        return None
    saved = checkpointer.get_tuple({"configurable": {"thread_id": str(conversation_id), "checkpoint_ns": ""}})
    if saved is None:
        return None
    checkpoint = saved.checkpoint
    event = checkpoint.get("channel_values", {}).get("_summarization_event")
    if not isinstance(event, Mapping):
        return None
    message = event.get("summary_message")
    content = extract_text(message.get("content", "") if isinstance(message, Mapping) else getattr(message, "content", ""))
    if not content:
        return None
    version = checkpoint.get("channel_versions", {}).get("_summarization_event")
    identity = f"{conversation_id}:root:{version}:{event.get('cutoff_index')}:{content}"
    path = event.get("file_path")
    return {
        "summary_id": hashlib.sha256(identity.encode()).hexdigest(),
        "content": content,
        # 最近 checkpoint 的时间可能是普通工具提交，不能冒充摘要创建时间。
        "created_at": None,
        "status": "persisted",
        "history_saved": bool(path) if not path or str(path).startswith("/conversation_history/") else None,
        "reason": "manual" if event.get("platform_reason") == "manual" else "automatic",
    }
