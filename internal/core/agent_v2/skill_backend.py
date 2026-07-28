"""Read-only BackendProtocol projection of builtin Skill packages."""

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


class SkillCatalogBackend(BackendProtocol):
    """Expose immutable SKILL.md files to native SkillsMiddleware."""

    def __init__(self, skills: list[Any]) -> None:
        self._files = {
            f"/{skill.name}/SKILL.md": skill.skill_md
            for skill in skills
            if skill.enabled
        }

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        content = self._files.get(self._normalize(file_path))
        if content is None:
            return ReadResult(error=f"skill file not found: {file_path}")
        lines = content.splitlines(keepends=True)
        return ReadResult(
            file_data={
                "content": "".join(lines[offset : offset + limit]),
                "encoding": "utf-8",
            }
        )

    def ls(self, path: str = "/") -> LsResult:
        normalized = self._normalize(path)
        entries: list[FileInfo] = []
        seen: set[str] = set()
        prefix = normalized.rstrip("/") + "/" if normalized != "/" else "/"
        for file_path in sorted(self._files):
            if not file_path.startswith(prefix):
                continue
            remainder = file_path[len(prefix) :]
            first = remainder.split("/", 1)[0]
            child = f"{prefix}{first}".replace("//", "/")
            if child in seen:
                continue
            seen.add(child)
            is_dir = "/" in remainder
            entries.append(
                {
                    "path": child + ("/" if is_dir else ""),
                    "is_dir": is_dir,
                    "size": 0 if is_dir else len(self._files[file_path].encode("utf-8")),
                    "modified_at": "",
                }
            )
        return LsResult(entries=entries)

    def ls_info(self, path: str = "/") -> list[FileInfo]:
        return self.ls(path).entries or []

    def glob(self, pattern: str, path: str | None = "/") -> GlobResult:
        prefix = self._normalize(path or "/").rstrip("/")
        matches = [
            {"path": file_path}
            for file_path in sorted(self._files)
            if (not prefix or file_path.startswith(prefix))
            and (
                fnmatch.fnmatch(file_path, pattern)
                or fnmatch.fnmatch(file_path.rsplit("/", 1)[-1], pattern)
            )
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
        prefix = self._normalize(path or "/").rstrip("/")
        matches: list[GrepMatch] = []
        for file_path, content in self._files.items():
            if prefix and not file_path.startswith(prefix):
                continue
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
        return WriteResult(error="Skill catalog is read-only")

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return EditResult(error="Skill catalog is read-only")

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        return [
            FileUploadResponse(path=path, error="Skill catalog is read-only")
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
                    error=None if content is not None else "skill file not found",
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


__all__ = ["SkillCatalogBackend"]
