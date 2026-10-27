"""Workspace 文件业务结果和展示规则。"""

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol


class WorkspaceFileView(Protocol):
    """预览规则所需的最小文件元数据视图。"""

    mime_type: str
    path: str


TEXT_PREVIEW_EXTENSIONS = frozenset(
    {
        ".csv",
        ".html",
        ".json",
        ".log",
        ".md",
        ".py",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)


def supports_text_preview(row: WorkspaceFileView) -> bool:
    """判断文件是否适合在接口中返回受限文本预览。"""
    mime_type = (row.mime_type or "").lower()
    return (
        mime_type.startswith("text/")
        or mime_type
        in {
            "application/json",
            "application/javascript",
            "application/xml",
            "application/yaml",
        }
        or PurePosixPath(row.path).suffix.lower() in TEXT_PREVIEW_EXTENSIONS
    )


@dataclass(frozen=True, slots=True)
class WorkspaceFilePreview:
    """前端文件预览结果。"""

    file: Any
    content: str | None = None
    download_url: str | None = None
    truncated: bool = False


__all__ = [
    "TEXT_PREVIEW_EXTENSIONS",
    "WorkspaceFilePreview",
    "WorkspaceFileView",
    "supports_text_preview",
]
