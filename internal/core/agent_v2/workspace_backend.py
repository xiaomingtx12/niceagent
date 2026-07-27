"""MyDifyWorkspaceBackend — DB 持久化的 deepagents FilesystemBackend。

约束（硬约束 #5）：
- 复用 deepagents `BackendProtocol`，不自己造工具协议
- 持久化走 my-dify 现有 `workspace_files` 表，不在磁盘或 COS 单独存

R1 路径白名单（[deepagents-v2-plan-workspace-three-layer-paths] memory）：
  /workspace/{account_id}/{app_id}/{conversation_id|seed}/<rest>
  - path 入参必须以 `/workspace/{account_id}/{app_id}/` 开头
  - 第二段允许 `seed` 或 == `conversation_id`，其他前缀拒绝
  - 拒绝 `..` / `~` / 绝对路径 / 越界

实现策略：
- 读路径：`ls_info` / `read` 走 SQL 查 version 最大行
- 写路径：`write` 走 `INSERT ... version+1` 软版本（每次写一个 snapshot）
- agent 视角的"路径"是 `/workspace/{account_id}/{app_id}/<rest>`，内部
  把 `<rest>` 的首段（seed / cnv_id）映射到 conversation_id
- 仅实现 FilesystemMiddleware 实际调用到的方法（read/write/edit/ls/glob/
  grep 同步+异步 + upload_files/download_files + 路径白名单校验），其余
  抛 NotImplementedError
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from deepagents.backends.protocol import (
    BackendProtocol,
    EditResult,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    GrepMatch,
    LsResult,
    ReadResult,
    WriteResult,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from internal.model import WorkspaceFile


class WorkspacePathError(ValueError):
    """路径白名单校验失败。"""


def _normalize(path: str) -> str:
    """路径标准化：去掉尾部 /，保留前导 /。"""
    if not path:
        return "/"
    p = path.rstrip("/")
    return p if p else "/"


def _validate_workspace_path(
    path: str,
    account_id: UUID,
    app_id: UUID,
    conversation_id: UUID,
) -> str:
    """校验 path 落在三层白名单内，返回内部相对路径 (account_id 前缀剥掉)。

    入参 path 期望形如 `/workspace/{account_id}/{app_id}/seed/<rest>` 或
    `/workspace/{account_id}/{app_id}/{conversation_id}/<rest>`。

    返回内部相对路径 (剥掉前缀)，形如 `seed/<rest>` 或 `{conversation_id}/<rest>`。

    校验规则：
    1. 必须以 `/workspace/` 开头
    2. 前缀 `/workspace/{account_id}/{app_id}/` 必须严格匹配
    3. 后续首段必须是 `seed`（跨会话共享）或 `conversation_id`（私有）
    4. 拒绝 `..` / `~` / `.` 段（任何位置）
    """
    p = _normalize(path)
    prefix = f"/workspace/{account_id}/{app_id}/"
    if not p.startswith(prefix):
        raise WorkspacePathError(
            f"path '{p}' outside workspace scope '{prefix}'"
        )
    rest = p[len(prefix):]
    if not rest:
        raise WorkspacePathError(
            f"path '{p}' missing scope segment (seed|conversation_id)"
        )
    segments = rest.split("/")
    first = segments[0]
    # 拒绝路径遍历 / home 展开 — 任何 segment 都不可为 '..' / '~' / '.'
    for seg in segments:
        if seg in (".", "..", "~"):
            raise WorkspacePathError(
                f"path '{p}' contains illegal segment '{seg}'"
            )
    # scope 校验
    if first == "seed":
        return rest
    if str(conversation_id) == first:
        return rest
    raise WorkspacePathError(
        f"path '{p}' scope '{first}' not allowed "
        f"(must be 'seed' or current conversation_id)"
    )


def _scope_to_conversation_id(rest: str, conversation_id: UUID) -> Optional[UUID]:
    """rest 首段 'seed' → None；== conversation_id → 返回；其他由 _validate 拒绝。"""
    first = rest.split("/", 1)[0]
    if first == "seed":
        return None
    if str(conversation_id) == first:
        return conversation_id
    return None


class MyDifyWorkspaceBackend(BackendProtocol):
    """deepagents FilesystemBackend 的 DB 持久化实现。

    参数：
        session: SQLAlchemy Session
        account_id / app_id / conversation_id: 三层路径 scope
    """

    def __init__(
        self,
        session: Session,
        account_id: UUID,
        app_id: UUID,
        conversation_id: UUID,
    ) -> None:
        self._session = session
        self._account_id = account_id
        self._app_id = app_id
        self._conversation_id = conversation_id

    # ---------- 路径校验 ----------

    def _resolve(self, path: str) -> str:
        """校验入参 path, 返回 rest 形式 (account_id 前缀已剥)。"""
        return _validate_workspace_path(
            path, self._account_id, self._app_id, self._conversation_id
        )

    # ---------- SQL 辅助 ----------

    def _fetch_latest(
        self, rest: str
    ) -> Optional[WorkspaceFile]:
        """取 (account_id, app_id, path=rest) version 最大的非删除行。"""
        stmt = (
            select(WorkspaceFile)
            .where(
                WorkspaceFile.account_id == self._account_id,
                WorkspaceFile.app_id == self._app_id,
                WorkspaceFile.path == rest,
                WorkspaceFile.is_deleted.is_(False),
            )
            .order_by(WorkspaceFile.version.desc())
            .limit(1)
        )
        return self._session.execute(stmt).scalar_one_or_none()

    def _fetch_current_version(self, rest: str) -> int:
        """当前 path 的最大 version (无行返回 0)。"""
        latest = self._fetch_latest(rest)
        return latest.version if latest else 0

    def _list_paths(self, prefix: str) -> list[WorkspaceFile]:
        """列 (account_id, app_id, path LIKE prefix%) 非删除行最新版本。"""
        like_pattern = f"{prefix}%"
        # 取每条 path 的 max(version)
        rows = self._session.execute(
            select(WorkspaceFile)
            .where(
                WorkspaceFile.account_id == self._account_id,
                WorkspaceFile.app_id == self._app_id,
                WorkspaceFile.path.like(like_pattern),
                WorkspaceFile.is_deleted.is_(False),
            )
            .order_by(WorkspaceFile.path, WorkspaceFile.version.desc())
        ).scalars().all()
        # 去重保留每 path 最新 version
        seen: dict[str, WorkspaceFile] = {}
        for r in rows:
            if r.path not in seen:
                seen[r.path] = r
        return list(seen.values())

    # ---------- BackendProtocol: read/write/edit ----------

    def read(
        self, file_path: str, offset: int = 0, limit: int = 2000
    ) -> ReadResult:
        try:
            rest = self._resolve(file_path)
        except WorkspacePathError as exc:
            return ReadResult(error=str(exc))
        row = self._fetch_latest(rest)
        if row is None or row.content is None:
            return ReadResult(error=f"file not found: {file_path}")
        content = row.content.decode("utf-8", errors="replace")
        # offset/limit 按行
        lines = content.splitlines(keepends=True)
        sliced = "".join(lines[offset : offset + limit])
        file_data: dict[str, Any] = {
            "content": sliced,
            "encoding": "utf-8",
        }
        if row.created_at:
            file_data["created_at"] = row.created_at.isoformat()
        if row.updated_at:
            file_data["modified_at"] = row.updated_at.isoformat()
        return ReadResult(file_data=file_data)  # type: ignore[arg-type]

    def write(self, file_path: str, content: str) -> WriteResult:
        try:
            rest = self._resolve(file_path)
        except WorkspacePathError as exc:
            return WriteResult(error=str(exc))
        new_version = self._fetch_current_version(rest) + 1
        conv_id = _scope_to_conversation_id(rest, self._conversation_id)
        row = WorkspaceFile(
            account_id=self._account_id,
            app_id=self._app_id,
            conversation_id=conv_id,
            path=rest,
            version=new_version,
            storage_backend="db",
            mime_type="text/plain",
            size_bytes=len(content.encode("utf-8")),
            content_sha256="",  # 简版: 不算 sha256 (R1.3 后续优化)
            content=content.encode("utf-8"),
            created_by=self._account_id,
            is_deleted=False,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self._session.add(row)
        self._session.flush()
        return WriteResult(path=file_path)

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        # edit 是 read + modify + write 的组合; 复用 read/write
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
            occurrences=existing.count(old_string) if replace_all else 1,
        )

    # ---------- BackendProtocol: ls/glob/grep ----------

    def ls(self, path: str = "/") -> LsResult:
        try:
            rest = self._resolve(path) if path and path != "/" else ""
        except WorkspacePathError as exc:
            return LsResult(error=str(exc))
        rows = self._list_paths(rest)
        entries: list[FileInfo] = [
            {"path": f"/workspace/{self._account_id}/{self._app_id}/{r.path}"}
            for r in rows
        ]
        return LsResult(entries=entries)

    def ls_info(self, path: str = "/") -> list[FileInfo]:
        result = self.ls(path)
        return result.entries or []

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        try:
            rest = self._resolve(path) if path and path != "/" else ""
        except WorkspacePathError as exc:
            return GlobResult(error=str(exc))
        rows = self._list_paths(rest)
        # 简化: 只做 "*" 通配
        if "*" not in pattern:
            pattern = f"*{pattern}*"
        import fnmatch
        matches: list[FileInfo] = []
        for r in rows:
            leaf = r.path.split("/")[-1]
            if fnmatch.fnmatch(leaf, pattern):
                matches.append(
                    {"path": f"/workspace/{self._account_id}/{self._app_id}/{r.path}"}
                )
        return GlobResult(matches=matches)

    def glob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        result = self.glob(pattern, path)
        return result.matches or []

    def grep(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> GrepResult:
        try:
            rest = self._resolve(path) if path else ""
        except WorkspacePathError as exc:
            return GrepResult(error=str(exc))
        rows = self._list_paths(rest)
        import fnmatch
        out_matches: list[GrepMatch] = []
        for r in rows:
            if glob and not fnmatch.fnmatch(
                r.path.split("/")[-1], glob
            ):
                continue
            if r.content is None:
                continue
            text = r.content.decode("utf-8", errors="replace")
            for ln, line in enumerate(text.splitlines(), 1):
                if pattern in line:
                    out_matches.append({
                        "path": f"/workspace/{self._account_id}/{self._app_id}/{r.path}",
                        "line": ln,
                        "text": line,
                    })
        return GrepResult(matches=out_matches)

    def grep_raw(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> list[GrepMatch] | str:
        result = self.grep(pattern, path, glob)
        if result.error:
            return result.error
        return result.matches or []

    # ---------- BackendProtocol: upload/download ----------

    def upload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        responses: list[FileUploadResponse] = []
        for path, content in files:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                responses.append(
                    FileUploadResponse(path=path, error=f"not utf-8: {exc}")
                )
                continue
            res = self.write(path, text)
            if res.error:
                responses.append(FileUploadResponse(path=path, error=res.error))
            else:
                responses.append(FileUploadResponse(path=res.path or path))
        return responses

    def aupload_files(
        self, files: list[tuple[str, bytes]]
    ) -> list[FileUploadResponse]:
        return self.upload_files(files)

    def download_files(
        self, paths: list[str]
    ) -> list[FileDownloadResponse]:
        responses: list[FileDownloadResponse] = []
        for p in paths:
            r = self.read(p)
            if r.error or r.file_data is None:
                responses.append(
                    FileDownloadResponse(path=p, error=r.error or "not found")
                )
                continue
            responses.append(
                FileDownloadResponse(
                    path=p,
                    content=(r.file_data["content"]).encode("utf-8"),
                )
            )
        return responses

    def adownload_files(
        self, paths: list[str]
    ) -> list[FileDownloadResponse]:
        return self.download_files(paths)

    # ---------- async 等价 ----------

    async def aread(
        self, file_path: str, offset: int = 0, limit: int = 2000
    ) -> ReadResult:
        return self.read(file_path, offset, limit)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return self.write(file_path, content)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return self.edit(file_path, old_string, new_string, replace_all)

    async def als(self, path: str = "/") -> LsResult:
        return self.ls(path)

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        return self.glob(pattern, path)

    async def agrep(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> GrepResult:
        return self.grep(pattern, path, glob)

    async def aglob_info(self, pattern: str, path: str = "/") -> list[FileInfo]:
        return self.glob_info(pattern, path)

    async def als_info(self, path: str = "/") -> list[FileInfo]:
        return self.ls_info(path)

    async def agrep_raw(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> list[GrepMatch] | str:
        return self.grep_raw(pattern, path, glob)


def build_workspace_backend(
    session: Session,
    account_id: UUID,
    app_id: UUID,
    conversation_id: UUID,
) -> MyDifyWorkspaceBackend:
    """构造 backend 工厂（factory 内调用）。

    参数都从 thread_config["metadata"] 注入，service 层 invoke 时填。
    """
    return MyDifyWorkspaceBackend(
        session=session,
        account_id=account_id,
        app_id=app_id,
        conversation_id=conversation_id,
    )


__all__ = [
    "MyDifyWorkspaceBackend",
    "WorkspacePathError",
    "build_workspace_backend",
]