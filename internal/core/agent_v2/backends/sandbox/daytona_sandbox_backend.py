"""Daytona Sandbox 的 Deep Agents 生命周期适配。"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Iterable
from functools import partial
from hashlib import sha256
from math import ceil
from inspect import signature
from typing import Any

from daytona import Daytona, DaytonaConfig, CreateSandboxFromSnapshotParams
from daytona.common.errors import DaytonaError
from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from langchain_daytona import DaytonaSandbox

from internal.core.agent_v2.observability.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)
from internal.core.agent_v2.backends.sandbox.sandbox_initialization import (
    SandboxProviderError,
    seed_sandbox_workspace,
)
from internal.core.agent_v2.backends.sandbox.sandbox_skill_upload import seed_sandbox_skills
from internal.core.agent_v2.backends.sandbox.session_sandbox_pool import DAYTONA_SESSION_POOL, SandboxLease
from internal.core.agent_v2.backends.workspace.workspace_projection import WorkspaceProjection


DAYTONA_FREE_CPU_LIMIT = 10
DAYTONA_FREE_MEMORY_GIB_LIMIT = 10
DAYTONA_FREE_DISK_GIB_LIMIT = 30
_DAYTONA_HOME = "/home/daytona"
# 框架大文件编辑会把 /tmp 编码进命令，必须保留 Sandbox 原生临时目录。
_VIRTUAL_ROOTS = ("workspace", "skills", "subagent_skills")


def _translate_virtual_path(path: str) -> str:
    """把单个 Agent 虚拟路径映射到 Daytona 用户目录。"""
    if path == "/":
        return _DAYTONA_HOME
    for name in _VIRTUAL_ROOTS:
        virtual_root = f"/{name}"
        if path == virtual_root:
            return f"{_DAYTONA_HOME}/{name}"
        if path.startswith(f"{virtual_root}/"):
            return f"{_DAYTONA_HOME}/{name}{path[len(virtual_root):]}"
    return path


def _restore_virtual_path(path: str) -> str:
    """把 Daytona 用户目录中的路径恢复为 Agent 虚拟路径。"""
    if path == _DAYTONA_HOME:
        return "/"
    for name in _VIRTUAL_ROOTS:
        physical_root = f"{_DAYTONA_HOME}/{name}"
        if path == physical_root:
            return f"/{name}"
        if path.startswith(f"{physical_root}/"):
            return f"/{name}{path[len(physical_root):]}"
    return path


def _restore_virtual_paths(text: str) -> str:
    """恢复错误文本或命令输出中的 Daytona 用户目录路径。"""
    for name in _VIRTUAL_ROOTS:
        text = text.replace(
            f"{_DAYTONA_HOME}/{name}",
            f"/{name}",
        )
    return text


def _restore_file_info_paths(entries: list[dict[str, Any]] | None) -> None:
    """就地恢复结构化文件结果中的物理路径。"""
    for entry in entries or []:
        path = entry.get("path")
        if isinstance(path, str):
            entry["path"] = _restore_virtual_path(path)


class DaytonaResourceQuotaExceeded(SandboxProviderError):
    """Daytona 免费套餐资源配额不足。"""

    def __init__(self, *, resource: str | None, detail: str) -> None:
        """保存超限维度，并给出可执行的套餐上限提示。"""
        self.resource = resource
        dimension = f"{resource} 已超出可用配额；" if resource else ""
        super().__init__(
            "Daytona 免费套餐资源配额不足："
            f"{dimension}套餐上限为 CPU {DAYTONA_FREE_CPU_LIMIT} 核、"
            f"内存 {DAYTONA_FREE_MEMORY_GIB_LIMIT} GiB、"
            f"存储 {DAYTONA_FREE_DISK_GIB_LIMIT} GiB。"
            f" Provider 返回：{detail}"
        )


class ManagedDaytonaSandbox(DaytonaSandbox):
    """一次 Run 独占的工作副本；会话可复用实例，但下一轮清空文件后重新使用。"""

    execution_uses_virtual_paths = True

    def __init__(
        self,
        *,
        sandbox: Any,
        timeout: int = 1800,
        lease: SandboxLease | None = None,
        idle_ttl: int = 600,
        telemetry: AgentTelemetry | None = None,
    ) -> None:
        """构造时固定租约归属，运行中只维护初始化和关闭状态。"""
        super().__init__(sandbox=sandbox, timeout=timeout)
        self._sandbox = sandbox
        self._telemetry = telemetry or NOOP_AGENT_TELEMETRY
        self._ready = False
        self._closed = False
        self._lease = lease
        self._idle_ttl = idle_ttl

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """固定执行目录并原样传递命令，路径字面量和程序源码不做替换。"""
        return super().execute(
            f"cd {shlex.quote(_DAYTONA_HOME)} && /bin/sh -c {shlex.quote(command)}",
            timeout=timeout,
        )

    def ls(self, path: str) -> LsResult:
        """列出文件时把 Daytona 物理路径恢复为 Agent 虚拟路径。"""
        result = super().ls(_translate_virtual_path(path))
        _restore_file_info_paths(result.entries)
        return result

    def read(
        self,
        file_path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> ReadResult:
        """读取文件时把虚拟路径转换为 Daytona 用户目录。"""
        result = super().read(
            _translate_virtual_path(file_path),
            offset=offset,
            limit=limit,
        )
        if result.error:
            result.error = _restore_virtual_paths(result.error)
        return result

    def write(self, file_path: str, content: str) -> WriteResult:
        """写入文件时保持 Deep Agents 返回的虚拟路径契约。"""
        result = super().write(_translate_virtual_path(file_path), content)
        if result.path:
            result.path = _restore_virtual_path(result.path)
        if result.error:
            result.error = _restore_virtual_paths(result.error)
        return result

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        """编辑文件时保持 Deep Agents 返回的虚拟路径契约。"""
        result = super().edit(
            _translate_virtual_path(file_path),
            old_string,
            new_string,
            replace_all=replace_all,
        )
        if result.path:
            result.path = _restore_virtual_path(result.path)
        if result.error:
            result.error = _restore_virtual_paths(result.error)
        return result

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
    ) -> GrepResult:
        """搜索文件时把匹配结果中的物理路径恢复为虚拟路径。"""
        result = super().grep(
            pattern,
            _translate_virtual_path(path or "/"),
            glob,
        )
        if result.matches:
            for match in result.matches:
                match["path"] = _restore_virtual_path(match["path"])
        if result.error:
            result.error = _restore_virtual_paths(result.error)
        return result

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        """匹配文件时把结果中的物理路径恢复为虚拟路径。"""
        result = super().glob(
            pattern,
            _translate_virtual_path(path or "/"),
        )
        _restore_file_info_paths(result.matches)
        if result.error:
            result.error = _restore_virtual_paths(result.error)
        return result

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """上传文件时把 Daytona 资源配额错误转换为稳定的业务异常。"""
        try:
            responses = super().upload_files(
                [(_translate_virtual_path(path), content) for path, content in files]
            )
            return [
                FileUploadResponse(path=path, error=response.error)
                for (path, _), response in zip(files, responses, strict=True)
            ]
        except DaytonaError as exc:
            detail = str(getattr(exc, "message", "") or exc).strip()
            if _is_daytona_quota_error(exc, detail):
                raise DaytonaResourceQuotaExceeded(
                    resource=_daytona_resource_dimension(exc, detail),
                    detail=detail or "unknown quota error",
                ) from exc
            raise

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """下载文件时把 Daytona 用户目录转换回虚拟路径。"""
        responses = super().download_files(
            [_translate_virtual_path(path) for path in paths]
        )
        return [
            FileDownloadResponse(
                path=path,
                content=response.content,
                error=response.error,
            )
            for path, response in zip(paths, responses, strict=True)
        ]

    async def als(self, path: str) -> LsResult:
        """父类异步实现不调用 ls，显式复用同一条路径映射。"""
        return await asyncio.to_thread(self.ls, path)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """保证异步读取和同步读取访问同一物理文件。"""
        return await asyncio.to_thread(self.read, file_path, offset, limit)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """复用同步映射与原生仅创建语义。"""
        return await asyncio.to_thread(self.write, file_path, content)

    async def aedit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False,
    ) -> EditResult:
        """避免父类把未映射路径编码后发送至远程命令。"""
        return await asyncio.to_thread(self.edit, file_path, old_string, new_string, replace_all)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        """异步匹配也恢复返回结果的虚拟路径。"""
        return await asyncio.to_thread(self.glob, pattern, path)

    async def agrep(
        self, pattern: str, path: str | None = None, glob: str | None = None,
    ) -> GrepResult:
        """搜索文本保持原样，仅映射独立的路径参数。"""
        return await asyncio.to_thread(self.grep, pattern, path, glob)

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """异步上传沿用路径、配额错误和逐项响应校验。"""
        return await asyncio.to_thread(self.upload_files, files)

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """异步下载沿用同一传输和路径恢复实现。"""
        return await asyncio.to_thread(self.download_files, paths)

    def close(self) -> None:
        """只归还或释放实例，不扫描文件；初始化失败的租约直接淘汰。"""
        if self._closed:
            return
        self._closed = True
        if self._lease is not None:
            DAYTONA_SESSION_POOL.release(self._lease, reusable=self._ready, ttl=self._idle_ttl)
        else:
            self._sandbox.stop()


def _ensure_daytona_auxiliary_directories(backend: ManagedDaytonaSandbox) -> None:
    """一次远程调用创建 Workspace、Skill 和临时文件目录。"""
    # execute 的工作目录已固定在用户目录，内部命令使用真实的相对路径。
    result = backend.execute("mkdir -p workspace skills subagent_skills")
    if result.exit_code not in (0, None):
        detail = str(getattr(result, "output", "") or "").strip()
        message = "failed to create Daytona Sandbox auxiliary directories"
        if detail:
            message = f"{message}: {detail[:500]}"
        raise SandboxProviderError(message)


def create_backend(
    *,
    settings: Any,
    workspace: WorkspaceProjection,
    sandbox_name: str,
    skill_files: Iterable[tuple[str, bytes]],
    telemetry: AgentTelemetry | None = None,
) -> ManagedDaytonaSandbox:
    """独占取得同一会话的 Sandbox，并刷新本轮临时文件投影。"""
    sandbox: Any | None = None
    backend: ManagedDaytonaSandbox | None = None
    lease: SandboxLease | None = None
    try:
        if getattr(settings, "AGENT_SANDBOX_SNAPSHOT_ID", None):
            raise SandboxProviderError(
                "AGENT_SANDBOX_SNAPSHOT_ID 仅适用于 langsmith Sandbox"
            )

        api_key = str(getattr(settings, "DAYTONA_API_KEY", "") or "").strip()
        api_url = str(getattr(settings, "DAYTONA_API_URL", "") or "").strip()
        client = Daytona(
            DaytonaConfig(
                api_key=api_key or None,
                api_url=api_url or None,
            )
        )
        ttl = int(getattr(settings, "AGENT_SANDBOX_IDLE_TTL_SECONDS", 600))
        execute_timeout = int(getattr(settings, "AGENT_SANDBOX_EXECUTE_TIMEOUT_SECONDS", 1800))
        credential_scope = sha256(f"{api_url}\0{api_key}".encode()).hexdigest()
        create_params = CreateSandboxFromSnapshotParams(
            auto_stop_interval=max(ceil(ttl / 60) + 1, ceil(execute_timeout / 60) + 1),
            auto_delete_interval=0,
        )

        lease, reused = DAYTONA_SESSION_POOL.acquire(
            (credential_scope, sandbox_name),
            create=partial(_create_remote, client, create_params),
            dispose=partial(_dispose_remote, client),
        )
        sandbox = lease.sandbox
        backend = ManagedDaytonaSandbox(
            sandbox=sandbox,
            timeout=execute_timeout,
            lease=lease,
            idle_ttl=ttl,
            telemetry=telemetry,
        )
        (telemetry or NOOP_AGENT_TELEMETRY).emit(
            "sandbox.session_acquire", attributes={"provider": "daytona", "reused": reused},
        )
        _initialize_workspace(backend, workspace, skill_files, reused=reused, telemetry=telemetry)
        backend._ready = True
        return backend
    except BaseException as exc:
        # 初始化中断同样清理 Provider，随后原样传播取消或退出信号。
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
        elif lease is not None:
            DAYTONA_SESSION_POOL.release(lease, reusable=False, ttl=0)
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, SandboxProviderError):
            raise
        if isinstance(exc, DaytonaError):
            detail = str(getattr(exc, "message", "") or exc).strip()
            if _is_daytona_quota_error(exc, detail):
                raise DaytonaResourceQuotaExceeded(
                    resource=_daytona_resource_dimension(exc, detail),
                    detail=detail or "unknown quota error",
                ) from exc
            message = "failed to create Daytona Sandbox"
            if detail:
                message = f"{message}: {detail}"
            raise SandboxProviderError(message) from exc
        raise SandboxProviderError("failed to create Daytona Sandbox") from exc


def delete_sandbox(name: str) -> None:
    """由会话删除入口撤销该会话的全部凭据作用域租约。"""
    DAYTONA_SESSION_POOL.invalidate(name)


def _create_remote(client: Daytona, params: CreateSandboxFromSnapshotParams) -> Any:
    """按调用签名兼容无参客户端，不把 SDK 内部 TypeError 当作创建重试依据。"""
    try:
        signature(client.create).bind(params)
    except TypeError:
        return client.create()
    except ValueError:
        # 无法反射签名时使用当前 SDK 契约，不重试可能已经创建资源的调用。
        pass
    return client.create(params)


def _dispose_remote(client: Daytona, resource: Any) -> None:
    """租约淘汰时优先删除远程资源，旧客户端仍可使用 stop。"""
    delete = getattr(client, "delete", None)
    if callable(delete):
        delete(resource)
    else:
        stop = getattr(resource, "stop", None)
        if callable(stop):
            stop()


def _initialize_workspace(
    backend: ManagedDaytonaSandbox,
    workspace: WorkspaceProjection,
    skill_files: Iterable[tuple[str, bytes]],
    *,
    reused: bool,
    telemetry: AgentTelemetry | None,
) -> None:
    """复用实例先清空旧投影，初始化成功后才允许结束时归还租约。"""
    if reused:
        result = backend.execute(
            "chmod -R u+w skills subagent_skills && "
            "rm -rf -- workspace skills subagent_skills"
        )
        if result.exit_code != 0:
            raise SandboxProviderError("failed to reset reused Daytona Sandbox")
    _ensure_daytona_auxiliary_directories(backend)
    seed_sandbox_workspace(backend, workspace=workspace)
    seed_sandbox_skills(
        backend, list(skill_files), projection_root=_DAYTONA_HOME,
        protect=True, compress=True, telemetry=telemetry,
    )


def _is_daytona_quota_error(exc: DaytonaError, detail: str) -> bool:
    """识别 Daytona API 的资源配额/容量错误，不误判认证和网络错误。"""
    text = " ".join(
        value.lower()
        for value in (detail, str(getattr(exc, "code", "") or ""))
        if value
    )
    if any(
        marker in text
        for marker in (
            "rate limit",
            "too many requests",
            "requests per",
        )
    ):
        return False
    return any(
        marker in text
        for marker in (
            "quota",
            "resource limit",
            "limit exceeded",
            "insufficient capacity",
            "capacity exceeded",
            "resource exhausted",
        )
    )


def _daytona_resource_dimension(exc: DaytonaError, detail: str) -> str | None:
    """从 Daytona 错误文本中提取 CPU、内存或存储维度。"""
    text = " ".join(
        value.lower()
        for value in (detail, str(getattr(exc, "code", "") or ""))
        if value
    )
    if any(marker in text for marker in ("cpu", "core", "cores", "vcpu")):
        return "CPU"
    if any(marker in text for marker in ("memory", "ram")):
        return "内存"
    if any(marker in text for marker in ("disk", "storage", "volume")):
        return "存储"
    return None
