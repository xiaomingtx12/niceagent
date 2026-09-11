"""文件记忆的提示词目录排版；只消费文件快照，不访问 Store。"""

from collections.abc import Iterable

from internal.core.agent_v2.entity.agent_memory import (
    AGENT_MEMORY_CATEGORIES,
    AGENT_MEMORY_INDEX,
    AGENT_MEMORY_VIRTUAL_ROOT,
    AgentMemoryFile,
)


_DIRECTORY_BUDGET_BYTES = 3500
_INDEX_BUDGET_BYTES = 3600
_PREFERENCE_FILE_MAX_BYTES = 900
_PREFERENCES_ROOT = f"{AGENT_MEMORY_VIRTUAL_ROOT}preferences/"
_DIRECTORY_OVERFLOW_HINT = "\n其余文件使用 ls /memories/ 查看。"


def render_memory_index(files: Iterable[AgentMemoryFile]) -> str:
    """按 UTF-8 字节预算列出路径并附上短偏好正文，保留同类文件的输入顺序。"""
    rows = sorted(files, key=lambda row: not row.path.startswith(_PREFERENCES_ROOT))
    parts = ["记忆目录（正文未列出的文件需要时使用 read_file 读取）：\n"]
    used_bytes = len(parts[0].encode("utf-8"))

    for row in rows:
        if row.path == AGENT_MEMORY_INDEX:
            continue
        folder = row.path.removeprefix(AGENT_MEMORY_VIRTUAL_ROOT).split("/")[0]
        category = AGENT_MEMORY_CATEGORIES.get(folder, "背景信息")
        line = f"\n- {row.path}（{category}）"
        line_bytes = len(line.encode("utf-8"))
        if used_bytes + line_bytes > _DIRECTORY_BUDGET_BYTES:
            parts.append(_DIRECTORY_OVERFLOW_HINT)
            used_bytes += len(_DIRECTORY_OVERFLOW_HINT.encode("utf-8"))
            break
        parts.append(line)
        used_bytes += line_bytes

    for row in rows:
        if not row.path.startswith(_PREFERENCES_ROOT):
            continue
        if row.size_bytes > _PREFERENCE_FILE_MAX_BYTES:
            continue
        block = f"\n\n偏好全文 {row.path}:\n{row.content}"
        block_bytes = len(block.encode("utf-8"))
        if used_bytes + block_bytes <= _INDEX_BUDGET_BYTES:
            parts.append(block)
            used_bytes += block_bytes

    return "".join(parts)
