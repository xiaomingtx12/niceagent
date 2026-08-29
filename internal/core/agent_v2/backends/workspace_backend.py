"""基于版本化记录的 DeepAgents 文件适配器。"""

from __future__ import annotations

import fnmatch
import base64
import mimetypes
from pathlib import PurePosixPath
from typing import Any, Optional
from uuid import UUID

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
from sqlalchemy.orm import Session

from internal.core.agent_v2.entity.workspace_path import (
    AgentWorkspacePathPolicy,
    build_workspace_path_digest,
)
from internal.core.agent_v2.persistence.workspace_file_repository import (
    WorkspaceFileRepository,
    WorkspaceFileRevisionConflict,
)
from internal.core.agent_v2.entity.workspace_quota import (
    DEFAULT_AGENT_WORKSPACE_LIMITS,
    AgentWorkspaceLimits,
)
from internal.exception import (
    WorkspacePathError,
    WorkspaceQuotaExceeded,
    WorkspaceStorageError,
)
from internal.core.agent_v2.assembly.observability.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)
from internal.model import WorkspaceFile
from internal.core.agent_v2.persistence.workspace_file_storage_protocol import (
    WorkspaceFileStorageProtocol,
)


class AgentWorkspaceBackend(BackendProtocol):
    """把作用域版本存储适配为 Backend。"""

    def __init__(
        self,
        session: Session,
        account_id: UUID,
        app_id: UUID,
        conversation_id: UUID,
        storage_service: WorkspaceFileStorageProtocol,
        created_by: Optional[UUID] = None,
        limits: AgentWorkspaceLimits = DEFAULT_AGENT_WORKSPACE_LIMITS,
        telemetry: AgentTelemetry | None = None,
    ) -> None:
        """绑定账号、应用和会话作用域。"""
        self._session = session
        self._account_id = account_id
        self._app_id = app_id
        self._conversation_id = conversation_id
        self._created_by = created_by
        self._telemetry = telemetry or NOOP_AGENT_TELEMETRY
        self._storage_service = storage_service
        self._paths = AgentWorkspacePathPolicy(
            conversation_id=conversation_id,
        )
        self._repository = WorkspaceFileRepository(
            session=session,
            account_id=account_id,
            app_id=app_id,
            conversation_id=conversation_id,
            created_by=created_by,
            limits=limits,
        )

    def close(self) -> None:
        """释放 Backend 独立数据库会话。"""
        self._repository.close()

    def _resolve(self, path: str) -> str:
        """把 Agent 路径解析为已校验存储路径。"""
        try:
            return self._paths.resolve(path)
        except WorkspacePathError as exc:
            self._telemetry.emit(
                "workspace.path_rejected",
                attributes={
                    "reason": "invalid",
                    "path_digest": build_workspace_path_digest(str(path)),
                },
            )
            raise

    def _virtual_path(self, rest: str) -> str:
        """把存储路径映射回 Agent 可见路径。"""
        return self._paths.virtual_path(rest)

    def _visible_prefixes(self) -> tuple[str, ...]:
        """返回当前 Backend 可见前缀。"""
        return self._paths.visible_prefixes()

    def _list_paths(self, prefix: Optional[str]) -> list[WorkspaceFile]:
        """列出路径下的最新记录。"""
        prefixes = (prefix,) if prefix is not None else self._visible_prefixes()
        return [
            row
            for row in self._repository.list(prefixes)
            if self._paths.is_visible_storage_path(row.path)
        ]

    def content_manifest(self) -> dict[str, str]:
        """返回当前会话文件路径到内容摘要的映射。"""
        return {
            self._virtual_path(row.path): row.content_sha256
            for row in self._list_paths(None)
        }

    def delete_files(self, paths: list[str]) -> list[str]:
        """删除一组已校验的 Workspace 文件并返回逐项错误。"""
        errors: list[str] = []
        for path in paths:
            try:
                rest = self._resolve(path)
                self._storage_service.delete(self._repository, rest)
            except (WorkspaceStorageError, WorkspacePathError) as exc:
                errors.append(f"{path}: {exc}")
        return errors

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """按协议读取文本片段或二进制 multimodal 数据。"""
        try:
            rest = self._resolve(file_path)
        except WorkspacePathError as exc:
            return ReadResult(error=str(exc))
        row = self._repository.fetch(rest)
        if row is None:
            return ReadResult(error=f"file not found: {file_path}")
        try:
            content_bytes = self._storage_service.read_bytes(row)
        except WorkspaceStorageError as exc:
            return ReadResult(error=str(exc))
        if self._is_binary(row):
            file_data: dict[str, Any] = {
                "content": base64.b64encode(content_bytes).decode("ascii"),
                "encoding": "base64",
            }
            if row.created_at:
                file_data["created_at"] = row.created_at.isoformat()
            if row.updated_at:
                file_data["modified_at"] = row.updated_at.isoformat()
            return ReadResult(file_data=file_data)  # type: ignore[arg-type]

        content = content_bytes.decode("utf-8", errors="replace")
        sliced = "".join(content.splitlines(keepends=True)[offset : offset + limit])
        file_data: dict[str, Any] = {"content": sliced, "encoding": "utf-8"}
        if row.created_at:
            file_data["created_at"] = row.created_at.isoformat()
        if row.updated_at:
            file_data["modified_at"] = row.updated_at.isoformat()
        return ReadResult(file_data=file_data)  # type: ignore[arg-type]

    @staticmethod
    def _is_binary(row: WorkspaceFile) -> bool:
        """按存储 MIME 和扩展名识别需要 base64 返回的文件。"""
        mime_type = (row.mime_type or "").lower()
        if mime_type.startswith(("text/", "image/", "audio/", "video/")):
            return not mime_type.startswith("text/")
        extension = PurePosixPath(row.path).suffix.lower()
        guessed = mimetypes.guess_type(f"file{extension}")[0] or ""
        return guessed.startswith(("image/", "audio/", "video/")) or extension in {
            ".pdf",
            ".doc",
            ".docx",
            ".xls",
            ".xlsx",
            ".ppt",
            ".pptx",
            ".zip",
        }

    def write(self, file_path: str, content: str) -> WriteResult:
        """为已校验路径追加文本快照。"""
        try:
            rest = self._resolve(file_path)
        except WorkspacePathError as exc:
            return WriteResult(error=str(exc))
        try:
            self._storage_service.write_text(
                self._repository,
                rest,
                content,
                telemetry=self._telemetry,
            )
        except (
            WorkspaceStorageError,
            WorkspaceQuotaExceeded,
            WorkspaceFileRevisionConflict,
        ) as exc:
            return WriteResult(error=str(exc))
        return WriteResult(path=file_path)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """替换文件中的一个或全部精确文本。"""
        read_result = self.read(file_path)
        if read_result.error:
            return EditResult(error=read_result.error)
        existing = read_result.file_data["content"]
        occurrences = existing.count(old_string)
        if occurrences == 0:
            return EditResult(error=f"old_string not found in {file_path}")
        if replace_all:
            new_content = existing.replace(old_string, new_string)
        else:
            if occurrences > 1:
                return EditResult(
                    error=(
                        f"old_string appears {occurrences} times; "
                        "pass replace_all=True or narrow the match"
                    )
                )
            new_content = existing.replace(old_string, new_string, 1)
        write_result = self.write(file_path, new_content)
        if write_result.error:
            return EditResult(error=write_result.error)
        return EditResult(
            path=file_path,
            occurrences=occurrences if replace_all else 1,
        )

    def ls(self, path: str = "/") -> LsResult:
        """列出可见 Workspace 路径下的文件。"""
        try:
            rest = self._resolve(path) if path and path != "/" else None
        except WorkspacePathError as exc:
            return LsResult(error=str(exc))
        entries: list[FileInfo] = [
            {"path": self._virtual_path(row.path)} for row in self._list_paths(rest)
        ]
        return LsResult(entries=entries)

    def ls_info(self, path: str = "/") -> list[FileInfo]:
        """返回协议兼容的原始列表。"""
        return self.ls(path).entries or []

    def glob(self, pattern: str, path: str | None = "/") -> GlobResult:
        """按相对路径或文件名匹配文件。"""
        try:
            rest = self._resolve(path) if path and path != "/" else None
        except WorkspacePathError as exc:
            return GlobResult(error=str(exc))
        matches: list[FileInfo] = []
        for row in self._list_paths(rest):
            candidate = (
                row.path[len(rest) + 1 :]
                if rest and row.path.startswith(f"{rest}/")
                else row.path
            )
            if fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch(
                candidate.rsplit("/", 1)[-1], pattern
            ):
                matches.append({"path": self._virtual_path(row.path)})
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
        """在可见 UTF-8 文件中查找文本。"""
        try:
            rest = self._resolve(path) if path and path != "/" else None
        except WorkspacePathError as exc:
            return GrepResult(error=str(exc))
        matches: list[GrepMatch] = []
        for row in self._list_paths(rest):
            if glob and not fnmatch.fnmatch(row.path.split("/")[-1], glob):
                continue
            try:
                content = self._storage_service.read_bytes(row)
            except WorkspaceStorageError:
                continue
            text = content.decode("utf-8", errors="replace")
            for line_number, line in enumerate(text.splitlines(), 1):
                if pattern not in line:
                    continue
                matches.append(
                    {
                        "path": self._virtual_path(row.path),
                        "line": line_number,
                        "text": line,
                    }
                )
                if max_count is not None and len(matches) > max_count:
                    return GrepResult(matches=matches[:max_count])
        return GrepResult(matches=matches)

    def grep_raw(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> list[GrepMatch] | str:
        """返回协议兼容的原始 Grep 结果。"""
        result = self.grep(pattern, path, glob)
        return result.error if result.error else result.matches or []

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """保存一批文本或二进制文件。"""
        responses: list[FileUploadResponse] = []
        for path, content in files:
            try:
                rest = self._resolve(path)
            except WorkspacePathError as exc:
                responses.append(FileUploadResponse(path=path, error=str(exc)))
                continue
            mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
            try:
                self._storage_service.write_bytes(
                    self._repository,
                    rest,
                    content,
                    mime_type=mime_type,
                    telemetry=self._telemetry,
                )
                result_path = path
                error = None
            except (
                WorkspaceStorageError,
                WorkspaceQuotaExceeded,
                WorkspaceFileRevisionConflict,
            ) as exc:
                result_path = path
                error = str(exc)
            responses.append(
                FileUploadResponse(
                    path=result_path,
                    error=error,
                )
            )
        return responses

    async def aupload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        """异步调用同步上传。"""
        return self.upload_files(files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """为可信适配器读取完整文件字节。"""
        responses: list[FileDownloadResponse] = []
        for path in paths:
            try:
                rest = self._resolve(path)
            except WorkspacePathError as exc:
                responses.append(FileDownloadResponse(path=path, error=str(exc)))
                continue
            row = self._repository.fetch(rest)
            if row is None:
                responses.append(FileDownloadResponse(path=path, error="not found"))
                continue
            try:
                content = self._storage_service.read_bytes(row)
            except WorkspaceStorageError as exc:
                responses.append(FileDownloadResponse(path=path, error=str(exc)))
                continue
            responses.append(
                FileDownloadResponse(
                    path=path,
                    content=content,
                )
            )
        return responses

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """异步调用同步下载。"""
        return self.download_files(paths)

    async def aread(
        self, file_path: str, offset: int = 0, limit: int = 2000
    ) -> ReadResult:
        """异步调用同步读取。"""
        return self.read(file_path, offset, limit)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """异步调用同步写入。"""
        return self.write(file_path, content)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """异步调用同步编辑。"""
        return self.edit(file_path, old_string, new_string, replace_all)

    async def als(self, path: str = "/") -> LsResult:
        """异步调用同步列表。"""
        return self.ls(path)

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        """异步调用同步 Glob。"""
        return self.glob(pattern, path)

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


from internal.core.agent_v2.backends.workspace_route_backend import (
    AgentWorkspaceRouteBackend,
)


__all__ = [
    "AgentWorkspaceBackend",
    "WorkspacePathError",
    "AgentWorkspaceRouteBackend",
]
