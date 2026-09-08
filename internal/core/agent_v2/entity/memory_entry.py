"""结构化长期记忆的内容、版本及检索规则。"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MemoryCategory = Literal["preference", "fact", "experience"]
MemoryStatus = Literal["active", "candidate", "paused", "forgotten"]
MemoryAction = Literal["add", "correct", "forget", "pause", "activate", "restore"]
MEMORY_TOKEN_BUDGET = 1800
MAX_MEMORY_ENTRIES = 100
MAX_MEMORY_REVISIONS = 20


class MemoryConflict(ValueError):
    """过期版本不能覆盖其他会话或用户的新修改。"""


class MemorySource(BaseModel):
    """来源身份由服务端注入，模型只能提供可核对的依据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["user", "agent"]
    evidence: str = Field(min_length=1, max_length=1000)
    conversation_id: str = ""
    message_id: str = ""
    operation_id: str = ""


class MemoryRevision(BaseModel):
    """每次变更保留完整快照，撤销仍产生一个新版本。"""

    model_config = ConfigDict(extra="forbid")
    version: int
    action: MemoryAction
    content: str
    status: MemoryStatus
    source: MemorySource
    updated_at: str


class MemoryEntry(BaseModel):
    """一个作用域内的单一主题；历史内容用于防止旧事实被重复加入。"""

    model_config = ConfigDict(extra="forbid")
    id: str
    topic: str = Field(min_length=1, max_length=100)
    scope: str = Field(default="当前应用", min_length=1, max_length=100)
    category: MemoryCategory
    content: str = Field(min_length=1, max_length=1000)
    status: MemoryStatus = "active"
    version: int = 1
    source: MemorySource
    created_at: str
    updated_at: str
    revisions: list[MemoryRevision] = Field(default_factory=list)


class MemoryChange(BaseModel):
    """统一表达工具和管理界面的变更，避免绕过版本校验。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: MemoryAction
    memory_id: str = Field(default="", max_length=64)
    expected_version: int = Field(default=0, ge=0, strict=True)
    topic: str = Field(default="", max_length=100)
    scope: str = Field(default="当前应用", min_length=1, max_length=100)
    category: MemoryCategory = "preference"
    content: str = Field(default="", max_length=1000)
    restore_version: int = Field(default=0, ge=0, strict=True)

    @field_validator("topic", "content", "scope")
    @classmethod
    def reject_credentials(cls, value: str) -> str:
        """拦截常见凭据格式，补充模型侧不保存凭据的规则。"""
        if re.search(
            r"-----BEGIN .*PRIVATE KEY-----|\bsk-[\w-]{20,}|\bBearer\s+\S{12,}|"
            r"(?:api[_ -]?key|password|access[_ -]?token|密码)\s*[:=：]\s*\S+",
            value, re.IGNORECASE,
        ):
            raise ValueError("记忆不能包含密码、令牌或 API 密钥")
        return value


def normalized_memory_text(value: str) -> str:
    """统一全半角、大小写和标点，阻止格式差异造成重复记忆。"""
    return "".join(c for c in unicodedata.normalize("NFKC", value).casefold() if c.isalnum())


def memory_terms(value: str) -> set[str]:
    """英文按词、中文按双字片段匹配，不依赖额外分词服务。"""
    value = unicodedata.normalize("NFKC", value).casefold()
    terms = set(re.findall(r"[a-z0-9_]{2,}", value))
    for segment in re.findall(r"[\u3400-\u9fff]+", value):
        terms.update(segment[i:i + 2] for i in range(len(segment) - 1))
    return terms


def estimate_memory_tokens(value: str) -> int:
    """使用偏保守的 UTF-8 字节估算；不把估算值冒充供应商计费 token。"""
    return math.ceil(len(value.encode("utf-8")) / 2)


def render_memory_entry(entry: MemoryEntry) -> str:
    """向模型提供可直接用于修正工具的条目身份与版本。"""
    return f"[{entry.id} v{entry.version} | {entry.category} | {entry.scope} | {entry.topic}]\n{entry.content}"


def select_memory_entries(
    entries: list[MemoryEntry], query: str, *, token_budget: int = MEMORY_TOKEN_BUDGET,
) -> tuple[list[MemoryEntry], str]:
    """优先相关条目，并为稳定偏好保留机会；候选、暂停和遗忘内容不参与注入。"""
    terms = memory_terms(query)
    ranked = []
    for entry in entries:
        if entry.status != "active":
            continue
        overlap = len(terms & memory_terms(f"{entry.topic} {entry.scope} {entry.content}"))
        if overlap or entry.category == "preference":
            ranked.append((overlap, entry.category == "preference", entry.updated_at, entry))
    ranked.sort(key=lambda item: item[:3], reverse=True)
    selected: list[MemoryEntry] = []
    body = ""
    for *_, entry in ranked:
        candidate = "\n\n".join(filter(None, [body, render_memory_entry(entry)]))
        if estimate_memory_tokens(candidate) <= token_budget:
            selected.append(entry)
            body = candidate
    return selected, body


def memory_now() -> str:
    """持久化统一使用 UTC，展示端按用户时区格式化。"""
    return datetime.now(timezone.utc).isoformat()
