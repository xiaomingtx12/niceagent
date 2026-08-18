"""虚拟目录的通用只读 Backend。"""
from __future__ import annotations

import fnmatch

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


class ReadOnlyCatalogBackend(BackendProtocol):
    """暴露 POSIX 路径到 UTF-8 文本的只读映射。"""

    def __init__(
        self,
        files: dict[str, str],
        *,
        missing_label: str,
        read_only_label: str,
    ) -> None:
        """初始化目录和错误标签。"""
        self._files = {self._normalize(path): content for path, content in files.items()}
        self._missing_label = missing_label
        self._read_only_label = read_only_label

    def read(
        self, file_path: str, offset: int = 0, limit: int = 2000
    ) -> ReadResult:
        """按行偏移读取 UTF-8 文件片段。"""
        content = self._files.get(self._normalize(file_path))
        if content is None:
            return ReadResult(error=f"{self._missing_label} not found: {file_path}")
        lines = content.splitlines(keepends=True)
        return ReadResult(
            file_data={
                "content": "".join(lines[offset : offset + limit]),
                "encoding": "utf-8",
            }
        )

    def ls(self, path: str = "/") -> LsResult:
        """列出虚拟目录的直接子项。"""
        normalized = self._normalize(path)
        if normalized in self._files:
            return LsResult(entries=[self._file_info(normalized)])
        prefix = normalized.rstrip("/") + "/" if normalized != "/" else "/"
        entries: list[FileInfo] = []
        seen: set[str] = set()
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
        """返回协议兼容的原始列表。"""
        return self.ls(path).entries or []

    def glob(self, pattern: str, path: str | None = "/") -> GlobResult:
        """按完整路径或文件名匹配目录文件。"""
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
        """返回协议兼容的原始 Glob 结果。"""
        return self.glob(pattern, path).matches or []

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        """在可见目录文件中查找文本。"""
        prefix = self._normalize(path or "/").rstrip("/")
        matches: list[GrepMatch] = []
        for file_path, content in self._files.items():
            if prefix and not file_path.startswith(prefix):
                continue
            if glob and not fnmatch.fnmatch(file_path.rsplit("/", 1)[-1], glob):
                continue
            for line_number, line in enumerate(content.splitlines(), 1):
                if pattern not in line:
                    continue
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
        """返回协议兼容的原始 Grep 结果。"""
        return self.grep(pattern, path, glob).matches or []

    def write(self, file_path: str, content: str) -> WriteResult:
        """拒绝写入只读目录。"""
        return WriteResult(error=f"{self._read_only_label} is read-only")

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """拒绝编辑只读目录。"""
        return EditResult(error=f"{self._read_only_label} is read-only")

    def upload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        """逐文件拒绝上传。"""
        return [
            FileUploadResponse(
                path=path,
                error=f"{self._read_only_label} is read-only",
            )
            for path, _ in files
        ]

    def download_files(
        self, paths: list[str]
    ) -> list[FileDownloadResponse]:
        """以 UTF-8 字节下载目录文件。"""
        responses: list[FileDownloadResponse] = []
        for path in paths:
            content = self._files.get(self._normalize(path))
            responses.append(
                FileDownloadResponse(
                    path=path,
                    content=content.encode("utf-8") if content is not None else None,
                    error=(
                        None
                        if content is not None
                        else f"{self._missing_label} not found"
                    ),
                )
            )
        return responses

    async def aread(
        self, file_path: str, offset: int = 0, limit: int = 2000
    ) -> ReadResult:
        """异步调用同步读取。"""
        return self.read(file_path, offset, limit)

    async def als(self, path: str = "/") -> LsResult:
        """异步调用同步列表。"""
        return self.ls(path)

    async def als_info(self, path: str = "/") -> list[FileInfo]:
        """异步返回原始列表。"""
        return self.ls_info(path)

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        """异步调用同步 Glob。"""
        return self.glob(pattern, path)

    async def aglob_info(
        self, pattern: str, path: str = "/"
    ) -> list[FileInfo]:
        """异步返回原始 Glob 结果。"""
        return self.glob_info(pattern, path)

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        """异步调用同步 Grep。"""
        return self.grep(pattern, path, glob, max_count=max_count)

    async def agrep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list[GrepMatch] | str:
        """异步返回原始 Grep 结果。"""
        return self.grep_raw(pattern, path, glob)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """异步调用拒绝写入。"""
        return self.write(file_path, content)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """异步调用拒绝编辑。"""
        return self.edit(file_path, old_string, new_string, replace_all)

    async def aupload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        """异步调用拒绝上传。"""
        return self.upload_files(files)

    async def adownload_files(
        self, paths: list[str]
    ) -> list[FileDownloadResponse]:
        """异步调用下载。"""
        return self.download_files(paths)

    def _file_info(self, path: str) -> FileInfo:
        """构造目录项元数据。"""
        return {
            "path": path,
            "is_dir": False,
            "size": len(self._files[path].encode("utf-8")),
            "modified_at": "",
        }

    @staticmethod
    def _normalize(path: str) -> str:
        """把目录路径规范为 POSIX 绝对路径。"""
        normalized = "/" + (path or "").replace("\\", "/").strip("/")
        return normalized if normalized else "/"


__all__ = ["ReadOnlyCatalogBackend"]
