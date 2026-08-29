"""CompositeBackend 的 Workspace 路由前缀适配器。"""

from __future__ import annotations

from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from internal.core.agent_v2.backends.workspace_backend import AgentWorkspaceBackend


class AgentWorkspaceRouteBackend(BackendProtocol):
    """为 CompositeBackend 恢复被剥离的 Workspace 路由前缀。"""

    def __init__(
        self,
        backend: AgentWorkspaceBackend,
        route_prefix: str,
        *,
        close_owner: bool = False,
    ) -> None:
        """绑定一个路由前缀；同一 Workspace 可以被多个路由共享。"""
        normalized = "/" + route_prefix.strip("/")
        if normalized == "/":
            raise ValueError("Workspace route prefix cannot be root")
        self._backend = backend
        self._prefix = normalized
        self._close_owner = close_owner

    def close(self) -> None:
        """仅由所有者路由关闭共享的 Workspace。"""
        if self._close_owner:
            self._backend.close()

    def _backend_path(self, path: str | None) -> str:
        normalized = "/" + (path or "/").lstrip("/")
        if normalized == "/":
            return self._prefix
        return f"{self._prefix}{normalized}"

    def _route_path(self, path: str) -> str:
        normalized = "/" + path.lstrip("/")
        if normalized == self._prefix:
            return "/"
        prefix = f"{self._prefix}/"
        if normalized.startswith(prefix):
            return normalized[len(self._prefix) :]
        return normalized

    def _remap_file_info(self, item: FileInfo) -> FileInfo:
        mapped = dict(item)
        if mapped.get("path"):
            mapped["path"] = self._route_path(str(mapped["path"]))
        return mapped

    def _remap_grep_match(self, item: GrepMatch) -> GrepMatch:
        mapped = dict(item)
        if mapped.get("path"):
            mapped["path"] = self._route_path(str(mapped["path"]))
        return mapped

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """把路由内相对路径映射到 Workspace Backend。"""
        return self._backend.read(
            self._backend_path(file_path),
            offset=offset,
            limit=limit,
        )

    def write(self, file_path: str, content: str) -> WriteResult:
        """写入路由内文件并保留 CompositeBackend 的原始路径。"""
        result = self._backend.write(self._backend_path(file_path), content)
        if result.path is not None:
            result.path = file_path
        return result

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """编辑路由内文件。"""
        result = self._backend.edit(
            self._backend_path(file_path),
            old_string,
            new_string,
            replace_all=replace_all,
        )
        if result.path is not None:
            result.path = file_path
        return result

    def ls(self, path: str = "/") -> LsResult:
        """列出路由目录并把结果转换为路由相对路径。"""
        result = self._backend.ls(self._backend_path(path))
        if result.error:
            return result
        return LsResult(
            entries=[self._remap_file_info(item) for item in (result.entries or [])]
        )

    def ls_info(self, path: str = "/") -> list[FileInfo]:
        """返回协议兼容的原始列表。"""
        return self.ls(path).entries or []

    def glob(self, pattern: str, path: str | None = "/") -> GlobResult:
        """匹配路由目录下的文件。"""
        result = self._backend.glob(pattern, self._backend_path(path))
        if result.error:
            return result
        return GlobResult(
            matches=[self._remap_file_info(item) for item in (result.matches or [])]
        )

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
        """在路由目录下搜索文本。"""
        result = self._backend.grep(
            pattern,
            self._backend_path(path),
            glob,
            max_count=max_count,
        )
        if result.error:
            return result
        return GrepResult(
            matches=[self._remap_grep_match(item) for item in (result.matches or [])]
        )

    def grep_raw(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> list[GrepMatch] | str:
        """返回协议兼容的原始 Grep 结果。"""
        result = self.grep(pattern, path, glob)
        return result.error if result.error else result.matches or []

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """批量上传路由内文件。"""
        mapped = [(self._backend_path(path), content) for path, content in files]
        responses = self._backend.upload_files(mapped)
        return [
            FileUploadResponse(
                path=files[index][0],
                error=response.error,
            )
            for index, response in enumerate(responses)
        ]

    async def aupload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        """异步批量上传路由内文件。"""
        return self.upload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """批量下载路由内文件。"""
        mapped = self._backend.download_files(
            [self._backend_path(path) for path in paths]
        )
        return [
            FileDownloadResponse(
                path=paths[index],
                content=response.content,
                error=response.error,
            )
            for index, response in enumerate(mapped)
        ]

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """异步批量下载路由内文件。"""
        return self.download_files(paths)

    async def aread(
        self, file_path: str, offset: int = 0, limit: int = 2000
    ) -> ReadResult:
        """异步读取路由内文件。"""
        return self.read(file_path, offset, limit)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """异步写入路由内文件。"""
        return self.write(file_path, content)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """异步编辑路由内文件。"""
        return self.edit(file_path, old_string, new_string, replace_all)

    async def als(self, path: str = "/") -> LsResult:
        """异步列出路由目录。"""
        return self.ls(path)

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        """异步匹配路由目录。"""
        return self.glob(pattern, path)

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        """异步搜索路由目录。"""
        return self.grep(pattern, path, glob, max_count=max_count)

    async def aglob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        """异步返回原始 Glob 结果。"""
        return self.glob_info(pattern, path)

    async def als_info(self, path: str = "/") -> list[FileInfo]:
        """异步返回原始列表。"""
        return self.ls_info(path)

    async def agrep_raw(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> list[GrepMatch] | str:
        """异步返回原始 Grep 结果。"""
        return self.grep_raw(pattern, path, glob)


__all__ = ["AgentWorkspaceRouteBackend"]
