"""Fail-closed BackendProtocol implementation for unrouted paths."""

from __future__ import annotations

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


class DenyBackend(BackendProtocol):
    """Reject every file operation.

    DeepAgents ``CompositeBackend`` delegates paths that do not match any route
    to its default backend. Production runtimes must reject those paths instead
    of falling back to a writable or executable workspace.
    """

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return ReadResult(error=f"path is not routed: {file_path}")

    def ls(self, path: str = "/") -> LsResult:
        return LsResult(error=f"path is not routed: {path}")

    def ls_info(self, path: str = "/") -> list[FileInfo]:
        return []

    def glob(self, pattern: str, path: str | None = "/") -> GlobResult:
        return GlobResult(error=f"path is not routed: {path or '/'}")

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        return []

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        return GrepResult(error=f"path is not routed: {path or '/'}")

    def grep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> list[GrepMatch] | str:
        return f"path is not routed: {path or '/'}"

    def write(self, file_path: str, content: str) -> WriteResult:
        return WriteResult(error=f"path is not writable: {file_path}")

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return EditResult(error=f"path is not editable: {file_path}")

    def upload_files(
        self,
        files: list[tuple[str, bytes]],
    ) -> list[FileUploadResponse]:
        return [
            FileUploadResponse(path=path, error=f"path is not writable: {path}")
            for path, _ in files
        ]

    def download_files(
        self,
        paths: list[str],
    ) -> list[FileDownloadResponse]:
        return [
            FileDownloadResponse(path=path, error=f"path is not routed: {path}")
            for path in paths
        ]

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


__all__ = ["DenyBackend"]
