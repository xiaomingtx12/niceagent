"""基于 Bubblewrap 的单机隔离 Sandbox Backend。

该实现适合部署在 Linux 云主机：Agent 进程仍在应用容器中，命令在独立的
mount、PID、IPC、UTS 和 network namespace 中执行。宿主环境变量不会传入
沙箱，只有 Workspace 可写，Skill 以只读挂载提供给命令。
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from typing import Any

from deepagents.backends.protocol import (
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

from internal.core.agent_v2.assembly.observability.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)
from internal.core.agent_v2.backends.sandbox.sandbox_workspace_sync import (
    SandboxProviderError,
    close_workspace_safely,
    seed_sandbox_workspace,
    sync_workspace_changes,
)
from internal.core.agent_v2.backends.workspace.workspace_backend import (
    AgentWorkspaceBackend,
)


class BubblewrapSandboxError(RuntimeError):
    """Bubblewrap 不可用或沙箱初始化失败。"""


class BubblewrapSandbox(BaseSandbox):
    """在临时目录和 Linux namespace 中执行命令。"""

    # BaseSandbox 的大文件 edit 会使用 /tmp；这里映射到本实例专属目录，
    # 避免暴露宿主机的真实临时目录。
    _MOUNTED_PATHS = ("workspace", "skills", "subagent_skills", "tmp")
    _READ_ONLY_SYSTEM_PATHS = ("/usr", "/bin", "/lib", "/lib64", "/sbin", "/etc")

    def __init__(
        self,
        *,
        allow_network: bool = False,
        timeout: int = 1800,
        max_output_bytes: int = 500 * 1024,
    ) -> None:
        """创建一次性沙箱目录；不继承宿主环境变量。"""
        if sys.platform != "linux":
            raise BubblewrapSandboxError("Bubblewrap Sandbox 仅支持 Linux")
        executable = shutil.which("bwrap")
        if executable is None:
            raise BubblewrapSandboxError("未找到 bwrap，请安装 bubblewrap")
        if timeout <= 0:
            raise ValueError("Sandbox execute timeout must be positive")
        if max_output_bytes <= 0:
            raise ValueError("Sandbox max output bytes must be positive")

        self._bwrap = executable
        self._root = Path(tempfile.mkdtemp(prefix="my-dify-sandbox-"))
        for name in self._MOUNTED_PATHS:
            (self._root / name).mkdir(parents=True, exist_ok=True)
        self._allow_network = allow_network
        self._default_timeout = timeout
        self._max_output_bytes = max_output_bytes
        self._sandbox_id = f"bwrap-{uuid.uuid4().hex[:16]}"
        self._closed = False

    @property
    def id(self) -> str:
        """返回本次沙箱的脱敏标识。"""
        return self._sandbox_id

    def close(self) -> None:
        """删除临时工作目录，清理动作幂等。"""
        if self._closed:
            return
        self._closed = True
        shutil.rmtree(self._root, ignore_errors=True)

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        """在隔离 namespace 中执行 Shell 命令并限制输出大小。"""
        if self._closed:
            return ExecuteResponse(
                output="Error: Sandbox is closed",
                exit_code=1,
            )
        if not isinstance(command, str) or not command.strip():
            return ExecuteResponse(
                output="Error: Command must be a non-empty string.",
                exit_code=1,
            )
        effective_timeout = self._default_timeout if timeout is None else timeout
        if effective_timeout <= 0:
            return ExecuteResponse(
                output="Error: timeout must be positive",
                exit_code=1,
            )

        args = self._build_command(command)
        process: subprocess.Popen[bytes] | None = None
        captured = bytearray()
        output_truncated = False
        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
                env={},
            )
            assert process.stdout is not None

            def drain_output() -> None:
                nonlocal output_truncated
                while True:
                    chunk = process.stdout.read(64 * 1024)
                    if not chunk:
                        return
                    remaining = self._max_output_bytes - len(captured)
                    if remaining > 0:
                        captured.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        output_truncated = True

            reader = threading.Thread(target=drain_output, daemon=True)
            reader.start()
            try:
                return_code = process.wait(timeout=effective_timeout)
            except subprocess.TimeoutExpired:
                self._terminate_process_group(process)
                reader.join()
                return ExecuteResponse(
                    output=(
                        f"Error: Command timed out after {effective_timeout} seconds"
                    ),
                    exit_code=124,
                )
            reader.join()

            raw = bytes(captured)
            if output_truncated:
                raw += b"\n\n... Output truncated due to sandbox output limits."
            return ExecuteResponse(
                output=raw.decode("utf-8", errors="replace"),
                exit_code=return_code,
                truncated=output_truncated,
            )
        except OSError as exc:
            return ExecuteResponse(
                output=f"Error executing sandbox command ({type(exc).__name__}): {exc}",
                exit_code=1,
            )

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """通过宿主文件传输 API 写入沙箱挂载目录。"""
        responses: list[FileUploadResponse] = []
        for path, content in files:
            try:
                target = self._resolve_host_path(path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            except (OSError, ValueError) as exc:
                responses.append(FileUploadResponse(path=path, error=str(exc)))
            else:
                responses.append(FileUploadResponse(path=path))
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """通过宿主文件传输 API 读取沙箱产物，逐文件返回错误。"""
        responses: list[FileDownloadResponse] = []
        for path in paths:
            try:
                source = self._resolve_host_path(path)
                if source.is_dir():
                    raise IsADirectoryError(path)
                content = source.read_bytes()
            except FileNotFoundError:
                responses.append(
                    FileDownloadResponse(path=path, error="file_not_found")
                )
            except (OSError, ValueError) as exc:
                responses.append(FileDownloadResponse(path=path, error=str(exc)))
            else:
                responses.append(FileDownloadResponse(path=path, content=content))
        return responses

    def _build_command(self, command: str) -> list[str]:
        """构建最小运行时挂载和清空环境变量的 bwrap 命令。"""
        args = [
            self._bwrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-user",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--clearenv",
        ]
        if not self._allow_network:
            args.append("--unshare-net")
        for path in self._READ_ONLY_SYSTEM_PATHS:
            source = Path(path)
            if source.exists():
                args.extend(("--ro-bind", path, path))
        args.extend(
            (
                "--bind",
                str(self._root / "workspace"),
                "/workspace",
                "--ro-bind",
                str(self._root / "skills"),
                "/skills",
                "--ro-bind",
                str(self._root / "subagent_skills"),
                "/subagent_skills",
                "--bind",
                str(self._root / "tmp"),
                "/tmp",
                "--chdir",
                "/workspace",
                "--setenv",
                "HOME",
                "/workspace",
                "--setenv",
                "PATH",
                "/usr/local/bin:/usr/bin:/bin",
                "/bin/sh",
                "-c",
                command,
            )
        )
        return args

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
        """超时后杀死整个命令组，避免子进程脱离请求生命周期。"""
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            process.kill()
        process.wait()

    def _resolve_host_path(self, path: str) -> Path:
        """把沙箱绝对路径映射到临时目录并阻断路径穿越/软链接逃逸。"""
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("sandbox paths must be absolute POSIX paths")
        posix = PurePosixPath(path)
        parts = posix.parts
        if len(parts) < 2 or parts[1] not in self._MOUNTED_PATHS:
            raise ValueError(f"path is outside sandbox mounts: {path}")
        if any(part in {"", ".", "..", "~"} for part in parts[2:]):
            raise ValueError(f"path contains an illegal segment: {path}")
        base = self._root / parts[1]
        target = base.joinpath(*parts[2:])
        resolved_base = base.resolve()
        resolved_target = target.resolve(strict=False)
        if not resolved_target.is_relative_to(resolved_base):
            raise ValueError(f"path escapes sandbox mount: {path}")
        return target


class ManagedBubblewrapSandbox(BubblewrapSandbox):
    """一次性本机 Sandbox，结束时将 Workspace 增量写回 COS。"""

    def __init__(
        self,
        *,
        workspace: AgentWorkspaceBackend,
        telemetry: AgentTelemetry | None = None,
        allow_network: bool = False,
        timeout: int = 1800,
        max_output_bytes: int = 500 * 1024,
    ) -> None:
        """创建绑定 Workspace 同步的 Bubblewrap Sandbox。"""
        super().__init__(
            allow_network=allow_network,
            timeout=timeout,
            max_output_bytes=max_output_bytes,
        )
        self._workspace = workspace
        self._telemetry = telemetry or NOOP_AGENT_TELEMETRY
        self._baseline_manifest: dict[str, str] = {}
        self._managed_closed = False

    def set_workspace_baseline(self, manifest: dict[str, str]) -> None:
        """记录运行开始时的权威 Workspace 摘要。"""
        self._baseline_manifest = dict(manifest)

    def close(self) -> None:
        """先同步增量产物，再关闭本机目录和 Workspace 会话。"""
        if self._managed_closed:
            return
        sync_error: Exception | None = None
        try:
            sync_workspace_changes(
                backend=self,
                workspace=self._workspace,
                baseline_manifest=self._baseline_manifest,
            )
        except Exception as exc:  # noqa: BLE001
            sync_error = exc
            self._telemetry.emit(
                "sandbox.workspace_sync_failed",
                attributes={"sandbox_id": self.id},
            )
        finally:
            try:
                super().close()
            except Exception as exc:  # noqa: BLE001
                if sync_error is None:
                    sync_error = exc
            workspace_close_error = close_workspace_safely(
                self._workspace,
                telemetry=self._telemetry,
                sandbox_id=self.id,
            )
            if sync_error is None and workspace_close_error is not None:
                sync_error = workspace_close_error
            self._managed_closed = True
        if sync_error is not None:
            raise SandboxProviderError(
                f"failed to close Sandbox {self.id}"
            ) from sync_error


def create_backend(
    *,
    settings: Any,
    workspace: AgentWorkspaceBackend,
    sandbox_name: str,
    skill_files: Iterable[tuple[str, bytes]],
    telemetry: AgentTelemetry | None = None,
) -> ManagedBubblewrapSandbox:
    """创建 Bubblewrap Sandbox，并播种 Workspace 与 Skill 文件。

    ``sandbox_name`` 只属于可复用的远程 Provider；这里接收它是为了让
    总工厂对所有 Provider 使用同一个创建契约。
    """
    del sandbox_name
    if getattr(settings, "AGENT_SANDBOX_SNAPSHOT_ID", None):
        raise SandboxProviderError(
            "AGENT_SANDBOX_SNAPSHOT_ID 仅适用于 langsmith Sandbox"
        )
    backend: ManagedBubblewrapSandbox | None = None
    try:
        network_setting = getattr(settings, "AGENT_SANDBOX_ALLOW_NETWORK", False)
        allow_network = (
            network_setting.lower() == "true"
            if isinstance(network_setting, str)
            else bool(network_setting)
        )
        backend = ManagedBubblewrapSandbox(
            workspace=workspace,
            telemetry=telemetry,
            allow_network=allow_network,
            timeout=int(
                getattr(settings, "AGENT_SANDBOX_EXECUTE_TIMEOUT_SECONDS", 1800)
            ),
            max_output_bytes=int(
                getattr(settings, "AGENT_SANDBOX_MAX_OUTPUT_BYTES", 500 * 1024)
            ),
        )
        seed_sandbox_workspace(
            backend,
            workspace=workspace,
            skill_files=list(skill_files),
        )
        return backend
    except Exception as exc:  # noqa: BLE001
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass
        close_workspace_safely(workspace, telemetry=telemetry)
        if isinstance(exc, SandboxProviderError):
            raise
        if isinstance(exc, BubblewrapSandboxError):
            raise SandboxProviderError(str(exc)) from exc
        raise SandboxProviderError("failed to create Bubblewrap Sandbox") from exc
