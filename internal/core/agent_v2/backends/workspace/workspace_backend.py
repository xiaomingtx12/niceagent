"""基于版本化记录的 DeepAgents 文件适配器。"""

from __future__ import annotations

import base64
import fnmatch
import mimetypes
from pathlib import PurePosixPath
from typing import TYPE_CHECKING
from uuid import UUID

from deepagents.backends.protocol import (
    BackendProtocol,
    EditResult,
    FileData,
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

from internal.core.agent_v2.assembly.observability.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
    build_path_digest,
)
from internal.core.agent_v2.entity.workspace_path import (
    AgentWorkspacePathPolicy,
    WorkspacePathError,
)
from internal.core.agent_v2.entity.workspace_quota import (
    AgentWorkspaceLimits,
    WorkspaceQuotaExceeded,
)
from internal.model import WorkspaceFile
from internal.repository.workspace_file_repository import (
    WorkspaceFileRepository,
    WorkspaceFileRevisionConflict,
    WorkspaceStorageError,
)

if TYPE_CHECKING:
    from internal.repository import WorkspaceFileStore


_WRITE_ERRORS = (
    WorkspaceStorageError,
    WorkspaceQuotaExceeded,
    WorkspaceFileRevisionConflict,
)


class AgentWorkspaceBackend(BackendProtocol):
    """把 Workspace 元数据和 COS 内容适配为 DeepAgents 文件 Backend。

    Backend 使用本次运行独立的数据库 Session；业务 Workspace Service 负责
    HTTP 查询和权限流程，两者不共享 Session 生命周期。
    """

    def __init__(
        self,
        session: Session,
        account_id: UUID,
        app_id: UUID,
        workspace_scope_id: UUID,
        file_store: WorkspaceFileStore,
        limits: AgentWorkspaceLimits,
        created_by: UUID | None = None,
        telemetry: AgentTelemetry | None = None,
    ) -> None:
        """绑定存储适配器、数据库仓储和当前 Workspace 作用域。"""
        self._telemetry = telemetry or NOOP_AGENT_TELEMETRY
        self._file_store = file_store
        self._paths = AgentWorkspacePathPolicy(
            scope_id=workspace_scope_id,
        )
        self._repository = WorkspaceFileRepository(
            session=session,
            account_id=account_id,
            app_id=app_id,
            scope_id=workspace_scope_id,
            created_by=created_by,
            limits=limits,
        )

    def close(self) -> None:
        """释放 Backend 独立数据库会话。"""
        self._repository.close()

    def _resolve_file(self, path: str) -> str:
        """把路由相对文件路径解析为当前作用域内的存储路径。"""
        return self._resolve(path, is_prefix=False)

    def _resolve_prefix(self, path: str) -> str:
        """把路由相对目录路径解析为当前作用域内的查询前缀。"""
        return self._resolve(path, is_prefix=True)

    def _resolve(self, path: str, *, is_prefix: bool) -> str:
        """规范 CompositeBackend 路径并记录被拒绝的输入。"""
        normalized = "/" + (path or "/").lstrip("/")
        if normalized != "/workspace" and not normalized.startswith("/workspace/"):
            normalized = f"/workspace{normalized}"
        try:
            if is_prefix:
                return self._paths.resolve_prefix(normalized)
            return self._paths.resolve_file(normalized)
        except WorkspacePathError as exc:
            self._telemetry.emit(
                "workspace.path_rejected",
                attributes={
                    "reason": "invalid",
                    "path_digest": build_path_digest(str(path)),
                },
            )
            raise

    def _relative_path(self, storage_path: str) -> str:
        """把存储路径转换为去掉 `/workspace` 前缀的可见路径。"""
        full_path = self._paths.virtual_path(storage_path)
        return full_path.removeprefix("/workspace") or "/"

    def _list_paths(self, prefix: str | None) -> list[WorkspaceFile]:
        """列出路径下的最新记录。"""
        prefixes = (prefix,) if prefix is not None else None
        rows = [
            row
            for row in self._repository.list(prefixes)
            if self._paths.contains(row.path)
        ]
        return sorted(rows, key=lambda row: row.path)

    def content_manifest(self) -> dict[str, str]:
        """返回 Sandbox 增量同步所需的 Workspace 文件摘要。"""
        return {
            f"/workspace{self._relative_path(row.path)}": row.content_sha256
            for row in self._list_paths(None)
        }

    def delete_files(self, paths: list[str]) -> list[str]:
        """删除 Sandbox 回写阶段确认已移除的文件，返回逐项错误。"""
        errors: list[str] = []
        for path in paths:
            try:
                rest = self._resolve_file(path)
                self._file_store.delete(self._repository, rest)
            except (WorkspaceStorageError, WorkspacePathError) as exc:
                errors.append(f"{path}: {exc}")
        return errors

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """按协议读取文本片段或二进制 multimodal 数据。"""
        if offset < 0 or limit < 0:
            return ReadResult(error="offset and limit must be non-negative")
        try:
            rest = self._resolve_file(file_path)
        except WorkspacePathError as exc:
            return ReadResult(error=str(exc))
        row = self._repository.fetch(rest)
        if row is None:
            return ReadResult(error=f"file not found: {file_path}")
        try:
            content_bytes = self._file_store.read_bytes(row)
        except WorkspaceStorageError as exc:
            return ReadResult(error=f"failed to read file '{file_path}': {exc}")
        if self._is_binary(row):
            file_data: FileData = {
                "content": base64.b64encode(content_bytes).decode("ascii"),
                "encoding": "base64",
            }
            if row.created_at:
                file_data["created_at"] = row.created_at.isoformat()
            if row.updated_at:
                file_data["modified_at"] = row.updated_at.isoformat()
            return ReadResult(file_data=file_data)

        content = content_bytes.decode("utf-8", errors="replace")
        sliced = "".join(content.splitlines(keepends=True)[offset : offset + limit])
        file_data: FileData = {"content": sliced, "encoding": "utf-8"}
        if row.created_at:
            file_data["created_at"] = row.created_at.isoformat()
        if row.updated_at:
            file_data["modified_at"] = row.updated_at.isoformat()
        return ReadResult(file_data=file_data)

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
        """为已校验路径写入文本快照。"""
        try:
            rest = self._resolve_file(file_path)
        except WorkspacePathError as exc:
            return WriteResult(error=str(exc))
        error = self._write_text(rest, content)
        if error is not None:
            return WriteResult(error=error)
        return WriteResult(path=file_path)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """替换文件中的一个或全部精确文本。"""
        try:
            rest = self._resolve_file(file_path)
        except WorkspacePathError as exc:
            return EditResult(error=str(exc))
        row = self._repository.fetch(rest)
        if row is None:
            return EditResult(error=f"file not found: {file_path}")
        if self._is_binary(row):
            return EditResult(error=f"cannot edit binary file: {file_path}")
        try:
            existing = self._file_store.read_bytes(row).decode("utf-8")
        except WorkspaceStorageError as exc:
            return EditResult(error=f"failed to read file '{file_path}': {exc}")
        except UnicodeDecodeError:
            return EditResult(error=f"cannot edit non-UTF-8 file: {file_path}")
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
        error = self._write_text(rest, new_content)
        if error is not None:
            return EditResult(error=error)
        return EditResult(
            path=file_path,
            occurrences=occurrences if replace_all else 1,
        )

    def ls(self, path: str = "/") -> LsResult:
        """列出可见 Workspace 路径下的文件。"""
        try:
            rest = self._resolve_prefix(path) if path and path != "/" else None
        except WorkspacePathError as exc:
            return LsResult(error=str(exc))
        entries: list[FileInfo] = [
            {"path": self._relative_path(row.path)} for row in self._list_paths(rest)
        ]
        return LsResult(entries=entries)

    def glob(self, pattern: str, path: str | None = "/") -> GlobResult:
        """按相对路径或文件名匹配文件。"""
        try:
            rest = self._resolve_prefix(path) if path and path != "/" else None
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
                matches.append({"path": self._relative_path(row.path)})
        return GlobResult(matches=matches)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        """在可见 UTF-8 文件中查找文本。"""
        try:
            rest = self._resolve_prefix(path) if path and path != "/" else None
        except WorkspacePathError as exc:
            return GrepResult(error=str(exc))
        matches: list[GrepMatch] = []
        for row in self._list_paths(rest):
            if self._is_binary(row):
                continue
            if glob and not fnmatch.fnmatch(row.path.split("/")[-1], glob):
                continue
            try:
                content = self._file_store.read_bytes(row)
            except WorkspaceStorageError as exc:
                return GrepResult(
                    error=(
                        f"failed to read file '{self._relative_path(row.path)}': {exc}"
                    )
                )
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                return GrepResult(
                    error=(
                        f"cannot search non-UTF-8 file "
                        f"'{self._relative_path(row.path)}'"
                    )
                )
            for line_number, line in enumerate(text.splitlines(), 1):
                if pattern not in line:
                    continue
                matches.append(
                    {
                        "path": self._relative_path(row.path),
                        "line": line_number,
                        "text": line,
                    }
                )
        return GrepResult(matches=matches)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """保存一批文本或二进制文件。"""
        responses: list[FileUploadResponse] = []
        for path, content in files:
            try:
                rest = self._resolve_file(path)
            except WorkspacePathError as exc:
                responses.append(FileUploadResponse(path=path, error=str(exc)))
                continue
            mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
            try:
                self._file_store.write_bytes(
                    self._repository,
                    rest,
                    content,
                    mime_type=mime_type,
                    telemetry=self._telemetry,
                )
                error = None
            except _WRITE_ERRORS as exc:
                error = f"failed to write file '{path}': {exc}"
            responses.append(
                FileUploadResponse(
                    path=path,
                    error=error,
                )
            )
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """为可信适配器读取完整文件字节。"""
        responses: list[FileDownloadResponse] = []
        for path in paths:
            try:
                rest = self._resolve_file(path)
            except WorkspacePathError as exc:
                responses.append(FileDownloadResponse(path=path, error=str(exc)))
                continue
            row = self._repository.fetch(rest)
            if row is None:
                responses.append(FileDownloadResponse(path=path, error="not found"))
                continue
            try:
                content = self._file_store.read_bytes(row)
            except WorkspaceStorageError as exc:
                responses.append(
                    FileDownloadResponse(
                        path=path,
                        error=f"failed to read file '{path}': {exc}",
                    )
                )
                continue
            responses.append(
                FileDownloadResponse(
                    path=path,
                    content=content,
                )
            )
        return responses

    def _write_text(self, storage_path: str, content: str) -> str | None:
        """写入文本并把可预期失败转换为稳定错误；其他异常继续抛出。"""
        try:
            self._file_store.write_text(
                self._repository,
                storage_path,
                content,
                telemetry=self._telemetry,
            )
        except _WRITE_ERRORS as exc:
            return f"failed to write file '{self._relative_path(storage_path)}': {exc}"
        return None


__all__ = ["AgentWorkspaceBackend"]
