"""Read-only long-term memory profile projection for DeepAgents."""

from __future__ import annotations

import fnmatch
from typing import Any

from deepagents.backends.protocol import (
    BackendProtocol,
    EditResult,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)


PROFILE_PATH = "/profile.md"


class MemoryProfileRenderer:
    """Render authoritative PostgreSQL facts into a stable model-facing file."""

    def render(self, facts: list[Any]) -> str:
        active = [
            fact
            for fact in facts
            if getattr(fact, "deleted_at", None) is None
            and str(getattr(fact, "content", "")).strip()
        ]
        if not active:
            return "# Long-term memory\n\nNo durable facts are available.\n"

        lines = ["# Long-term memory", ""]
        for fact in active:
            fact_type = str(getattr(fact, "fact_type", "general") or "general")
            content = " ".join(str(fact.content).strip().split())
            lines.append(f"- [{fact_type}] {content}")
        lines.append("")
        return "\n".join(lines)


class MemoryProfileBackend(BackendProtocol):
    """Expose ``/memories/profile.md`` as a read-only Backend route."""

    def __init__(self, content: str) -> None:
        self._files = {PROFILE_PATH: content}

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        content = self._files.get(self._normalize(file_path))
        if content is None:
            return ReadResult(error=f"memory file not found: {file_path}")
        lines = content.splitlines(keepends=True)
        return ReadResult(
            file_data={
                "content": "".join(lines[offset : offset + limit]),
                "encoding": "utf-8",
            }
        )

    def ls(self, path: str = "/") -> LsResult:
        normalized = self._normalize(path)
        if normalized == "/":
            return LsResult(
                entries=[
                    {
                        "path": PROFILE_PATH,
                        "is_dir": False,
                        "size": len(self._files[PROFILE_PATH].encode("utf-8")),
                        "modified_at": "",
                    }
                ]
            )
        if normalized == PROFILE_PATH:
            return LsResult(
                entries=[
                    {
                        "path": PROFILE_PATH,
                        "is_dir": False,
                        "size": len(self._files[PROFILE_PATH].encode("utf-8")),
                        "modified_at": "",
                    }
                ]
            )
        return LsResult(error=f"memory path not found: {path}")

    def ls_info(self, path: str = "/") -> list[FileInfo]:
        return self.ls(path).entries or []

    def glob(self, pattern: str, path: str | None = "/") -> GlobResult:
        matches = [
            {"path": file_path}
            for file_path in sorted(self._files)
            if fnmatch.fnmatch(file_path, pattern)
            or fnmatch.fnmatch(file_path.rsplit("/", 1)[-1], pattern)
        ]
        return GlobResult(matches=matches)

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        return self.glob(pattern, path).matches or []

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        matches: list[GrepMatch] = []
        for file_path, content in self._files.items():
            if glob and not fnmatch.fnmatch(file_path.rsplit("/", 1)[-1], glob):
                continue
            for line_number, line in enumerate(content.splitlines(), 1):
                if pattern in line:
                    matches.append(
                        {"path": file_path, "line": line_number, "text": line}
                    )
                    if max_count and len(matches) >= max_count:
                        return GrepResult(matches=matches)
        return GrepResult(matches=matches)

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list[GrepMatch] | str:
        return self.grep(pattern, path, glob).matches or []

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error="Memory profile is read-only")

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return EditResult(error="Memory profile is read-only")

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        return [
            FileUploadResponse(path=path, error="Memory profile is read-only")
            for path, _ in files
        ]

    def download_files(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        results: list[FileDownloadResponse] = []
        for path in paths:
            content = self._files.get(self._normalize(path))
            results.append(
                FileDownloadResponse(
                    path=path,
                    content=content.encode("utf-8") if content is not None else None,
                    error=None if content is not None else "memory file not found",
                )
            )
        return results

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000):
        return self.read(file_path, offset, limit)

    async def als(self, path: str = "/"):
        return self.ls(path)

    async def als_info(self, path: str = "/"):
        return self.ls_info(path)

    async def aglob(self, pattern: str, path: str = "/"):
        return self.glob(pattern, path)

    async def aglob_info(self, pattern: str, path: str = "/"):
        return self.glob_info(pattern, path)

    async def agrep(self, pattern: str, path=None, glob=None, *, max_count=None):
        return self.grep(pattern, path, glob, max_count=max_count)

    async def agrep_raw(self, pattern: str, path=None, glob=None):
        return self.grep_raw(pattern, path, glob)

    async def awrite(self, file_path: str, content: str):
        return self.write(file_path, content)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ):
        return self.edit(file_path, old_string, new_string, replace_all)

    async def aupload_files(self, files):
        return self.upload_files(files)

    async def adownload_files(self, paths):
        return self.download_files(paths)

    @staticmethod
    def _normalize(path: str) -> str:
        normalized = "/" + (path or "").replace("\\", "/").strip("/")
        return normalized if normalized != "" else "/"


__all__ = ["MemoryProfileBackend", "MemoryProfileRenderer", "PROFILE_PATH"]
