"""让远程 Sandbox 的创建和文件播种退出普通问答的启动路径。"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from threading import RLock
from typing import Any

from deepagents.backends import CompositeBackend
from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    SandboxBackendProtocol,
    WriteResult,
    execute_accepts_timeout,
)

from internal.core.agent_v2.backends.sandbox.sandbox_initialization import SandboxProviderError
from internal.core.agent_v2.backends.workspace.workspace_projection import WorkspaceProjection
from internal.core.agent_v2.observability.telemetry import AgentTelemetry, NOOP_AGENT_TELEMETRY
from internal.extension.logging_extension import logger


class LazySandboxBackend(SandboxBackendProtocol):
    """启动前使用本轮本地副本，启动后文件工具与命令统一访问远程副本。

    锁保护本地操作、首次初始化和关闭，远程工具仍可并行。
    Workspace 始终由运行资源持有，包装器与 Provider 均只借用。
    """

    def __init__(
        self,
        *,
        creator: Callable[[], SandboxBackendProtocol],
        workspace: WorkspaceProjection,
        sandbox_name: str,
        provider: str,
        telemetry: AgentTelemetry | None = None,
    ) -> None:
        """只保存资源工厂，不建立连接、不导出文件或上传 Skill。"""
        self._creator = creator
        self._workspace = workspace
        # 仅对已确认的 /workspace 路径使用此路由，复用官方路径重映射。
        self._local = CompositeBackend(default=workspace.backend, routes={"/workspace/": workspace.backend})
        self._sandbox_name = sandbox_name
        self._provider = provider
        self.execution_uses_virtual_paths = provider == "daytona"
        self._telemetry = telemetry or NOOP_AGENT_TELEMETRY
        self._backend: SandboxBackendProtocol | None = None
        self._error: BaseException | None = None
        self._closed = False
        self._lock = RLock()

    @property
    def id(self) -> str:
        """框架读取标识不应隐式创建远程资源。"""
        with self._lock:
            return self._backend.id if self._backend is not None else self._sandbox_name

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """首个命令触发一次初始化，后续命令共享同一个隔离文件系统。"""
        backend = self._ensure_backend()
        if timeout is not None and execute_accepts_timeout(type(backend)):
            return backend.execute(command, timeout=timeout)
        return backend.execute(command)

    def ls(self, path: str) -> LsResult:
        """启动前只列出虚拟根目录，避免探索可见目录时启动沙箱。"""
        with self._lock:
            self._check_available()
            if self._backend is None and path == "/":
                return LsResult(entries=[
                    {"path": root, "is_dir": True}
                    for root in ("/workspace/", "/skills/", "/subagent_skills/", "/tmp/")
                ])
        return self._file_call("ls", [path], path)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """Workspace 阅读无需创建沙箱，启动后则读取命令产生的最新内容。"""
        return self._file_call("read", [file_path], file_path, offset, limit)

    def write(self, file_path: str, content: str) -> WriteResult:
        """启动前的写入会随首次初始化迁移，启动后只写入 Sandbox。"""
        return self._file_call("write", [file_path], file_path, content)

    def edit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False,
    ) -> EditResult:
        """让编辑与其他文件工具遵守相同的初始化边界。"""
        return self._file_call("edit", [file_path], file_path, old_string, new_string, replace_all)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """仅查 Workspace 时走本地存储，跨根目录查询才需要沙箱。"""
        return self._file_call("glob", [path or "/"], pattern, path)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        """搜索与读取使用相同的数据源，避免启动前后出现陈旧结果。"""
        return self._file_call("grep", [path or "/"], pattern, path, glob)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """附件先进入本轮工作副本，不因附件准备提前启动远程沙箱。"""
        if not files:
            return []
        return self._file_call("upload_files", [path for path, _ in files], files)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """批量读取保持输入路径和顺序，混合沙箱路径时整体切换。"""
        if not paths:
            return []
        return self._file_call("download_files", paths, paths)

    def close(self) -> None:
        """只关闭已创建的 Provider，借用的 Workspace 由运行资源最后释放。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._backend is not None:
                # 原生 LangSmith Backend 没有 close；只有持有运行级资源的 Provider 需要释放。
                close = getattr(self._backend, "close", None)
                if callable(close):
                    close()
            elif self._error is None:
                self._telemetry.emit("sandbox.initialization_skipped", attributes={"provider": self._provider})
            # 初始化失败由 creator 清理远程资源，不能再次尝试创建。

    def _file_call(self, method: str, paths: Sequence[str], *args: Any) -> Any:
        """锁住本地操作直到完成，防止首次播种与附件写入交错。"""
        with self._lock:
            self._check_available()
            if self._backend is None and all(
                path == "/workspace" or path.startswith("/workspace/") for path in paths
            ):
                return getattr(self._local, method)(*args)
            backend = self._ensure_backend()
        return getattr(backend, method)(*args)

    def _ensure_backend(self) -> SandboxBackendProtocol:
        """合并并发首次调用，并单独记录从首字等待中移出的初始化耗时。"""
        with self._lock:
            self._check_available()
            if self._backend is not None:
                return self._backend
            started_at = time.monotonic()
            status = "completed"
            try:
                self._backend = self._creator()
                return self._backend
            except BaseException as exc:
                status = "failed"
                self._error = exc
                raise
            finally:
                duration_ms = (time.monotonic() - started_at) * 1000
                self._telemetry.emit(
                    "sandbox.initialize", value=duration_ms,
                    attributes={"provider": self._provider, "status": status, "unit": "ms"},
                )
                logger.info(
                    "agent_sandbox_timing sandbox=%s provider=%s stage=initialize status=%s duration_ms=%.2f",
                    self._sandbox_name, self._provider, status, duration_ms,
                )

    def _check_available(self) -> None:
        """拒绝继续使用已关闭或初始化失败的本轮副本。"""
        if self._closed:
            raise SandboxProviderError("Sandbox backend is closed")
        if self._error is not None:
            raise SandboxProviderError("Sandbox initialization previously failed") from self._error
