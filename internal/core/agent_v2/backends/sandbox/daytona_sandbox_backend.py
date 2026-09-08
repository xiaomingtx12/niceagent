"""Daytona Sandbox 的 Deep Agents 生命周期适配。"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from daytona import Daytona, DaytonaConfig
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
from internal.core.agent_v2.backends.sandbox.sandbox_workspace_sync import (
    SandboxProviderError,
    close_workspace_safely,
    ensure_sandbox_workspace,
    seed_sandbox_workspace,
)
from internal.core.agent_v2.backends.sandbox.managed_sandbox import (
    close_managed_sandbox,
)
from internal.core.agent_v2.backends.workspace.workspace_backend import WorkspaceBackend
from internal.core.agent_v2.entity.workspace_snapshot import WorkspaceManifest


DAYTONA_FREE_CPU_LIMIT = 10
DAYTONA_FREE_MEMORY_GIB_LIMIT = 10
DAYTONA_FREE_DISK_GIB_LIMIT = 30
_DAYTONA_HOME = "/home/daytona"
_VIRTUAL_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_:/])/(?:workspace|skills|subagent_skills|tmp)(?=/|\s|['\"`]|$)"
)
_VIRTUAL_ROOTS = ("workspace", "skills", "subagent_skills", "tmp")


def _translate_virtual_path(path: str) -> str:
    """把单个 Agent 虚拟路径映射到 Daytona 用户目录。"""
    for name in _VIRTUAL_ROOTS:
        virtual_root = f"/{name}"
        if path == virtual_root:
            return f"{_DAYTONA_HOME}/{name}"
        if path.startswith(f"{virtual_root}/"):
            return f"{_DAYTONA_HOME}/{name}{path[len(virtual_root):]}"
    return path


def _translate_virtual_paths(command: str) -> str:
    """把命令中的常用 Agent 虚拟目录映射到 Daytona 用户目录。"""

    def replace(match: re.Match[str]) -> str:
        return _translate_virtual_path(match.group(0))

    return _VIRTUAL_PATH_PATTERN.sub(replace, command)


def _restore_virtual_path(path: str) -> str:
    """把 Daytona 用户目录中的路径恢复为 Agent 虚拟路径。"""
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
    """一次 Run 独占的 Daytona Sandbox，结束时同步并停止远程资源。

    Daytona Sandbox 不跨对话复用；Workspace/COS 是持久化权威来源，远程
    Sandbox 只承载当前 Run 的临时执行状态。
    """

    def __init__(
        self,
        *,
        sandbox: Any,
        workspace: WorkspaceBackend,
        timeout: int = 1800,
        telemetry: AgentTelemetry | None = None,
    ) -> None:
        super().__init__(sandbox=sandbox, timeout=timeout)
        self._sandbox = sandbox
        self._workspace = workspace
        self._telemetry = telemetry or NOOP_AGENT_TELEMETRY
        self._baseline_manifest: WorkspaceManifest | None = None
        self._closed = False

    def set_workspace_baseline(self, manifest: WorkspaceManifest) -> None:
        """记录运行开始时的权威 Workspace 清单。"""
        self._baseline_manifest = dict(manifest)

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """在 Daytona 可写的用户目录中执行 Agent 的虚拟路径命令。"""
        return super().execute(
            _translate_virtual_paths(command),
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
            _translate_virtual_path(path) if path else None,
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
            _translate_virtual_path(path) if path else None,
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
                for (path, _), response in zip(files, responses, strict=False)
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
            for path, response in zip(paths, responses, strict=False)
        ]

    def close(self) -> None:
        """同步 Workspace 后停止 Daytona Sandbox；清理动作幂等。"""
        if self._closed:
            return
        try:
            close_managed_sandbox(
                backend=self,
                workspace=self._workspace,
                baseline_manifest=self._baseline_manifest,
                close_provider=self._sandbox.stop,
                telemetry=self._telemetry,
                sandbox_id=self.id,
                provider_name="Daytona",
            )
        finally:
            self._closed = True


def _ensure_daytona_auxiliary_directories(backend: ManagedDaytonaSandbox) -> None:
    """创建 Skill 和临时文件的可写目录。"""
    result = backend.execute("mkdir -p /skills /subagent_skills /tmp")
    if result.exit_code not in (0, None):
        detail = str(getattr(result, "output", "") or "").strip()
        message = "failed to create Daytona Sandbox auxiliary directories"
        if detail:
            message = f"{message}: {detail[:500]}"
        raise SandboxProviderError(message)


def create_backend(
    *,
    settings: Any,
    workspace: WorkspaceBackend,
    sandbox_name: str,
    skill_files: Iterable[tuple[str, bytes]],
    telemetry: AgentTelemetry | None = None,
) -> ManagedDaytonaSandbox:
    """创建一次性 Daytona Sandbox，并播种 Workspace 与 Skill 文件。"""
    del sandbox_name
    sandbox: Any | None = None
    backend: ManagedDaytonaSandbox | None = None
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
        sandbox = client.create()
        backend = ManagedDaytonaSandbox(
            sandbox=sandbox,
            workspace=workspace,
            timeout=int(
                getattr(settings, "AGENT_SANDBOX_EXECUTE_TIMEOUT_SECONDS", 1800)
            ),
            telemetry=telemetry,
        )
        ensure_sandbox_workspace(backend)
        _ensure_daytona_auxiliary_directories(backend)
        seed_sandbox_workspace(
            backend,
            workspace=workspace,
            skill_files=skill_files,
            protect_skills=True,
        )
        return backend
    except Exception as exc:  # noqa: BLE001
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
        elif sandbox is not None:
            try:
                sandbox.stop()
            except Exception:
                telemetry_obj = telemetry or NOOP_AGENT_TELEMETRY
                telemetry_obj.emit(
                    "sandbox.stop_failed",
                    attributes={"sandbox_id": getattr(sandbox, "id", None)},
                )
            close_workspace_safely(
                workspace,
                telemetry=telemetry,
                sandbox_id=getattr(sandbox, "id", None),
            )
        else:
            close_workspace_safely(workspace, telemetry=telemetry)
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
