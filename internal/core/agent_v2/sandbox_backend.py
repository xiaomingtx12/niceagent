"""OS-isolated command execution over the DB-backed workspace."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol

from .workspace_backend import MyDifyWorkspaceBackend

_MAX_OUTPUT = 100_000
_MAX_COMMAND_LENGTH = 8192
_AUDIT_WRAPPER = r"""
import os
from pathlib import Path
import runpy
import sys

root = Path(sys.argv[1]).resolve()
script = Path(sys.argv[2]).resolve()
script_args = sys.argv[3:]
runtime_roots = [Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve()]

def inside(path):
    resolved = Path(path).resolve()
    return root == resolved or root in resolved.parents

def require_inside(path, message):
    if isinstance(path, (str, bytes, os.PathLike)) and not inside(path):
        raise PermissionError(message)

def audit(event, args):
    if event in {
        "subprocess.Popen", "os.system", "os.spawn", "os.startfile",
        "socket.connect", "socket.bind",
    }:
        raise PermissionError(f"sandbox blocked audit event: {event}")
    if event.startswith("ctypes.") or event.startswith("winreg."):
        raise PermissionError(f"sandbox blocked audit event: {event}")
    if event == "open" and args:
        path = args[0]
        mode = args[1] if len(args) > 1 and isinstance(args[1], str) else "r"
        if isinstance(path, (str, bytes, os.PathLike)):
            resolved = Path(path).resolve()
            writing = any(flag in mode for flag in ("w", "a", "x", "+"))
            if writing and not inside(resolved):
                raise PermissionError("sandbox writes must stay inside workspace")
            if not writing and not inside(resolved):
                if not any(base == resolved or base in resolved.parents for base in runtime_roots):
                    raise PermissionError("sandbox reads must stay inside workspace")
    if event in {
        "os.remove", "os.rmdir", "os.mkdir", "os.chdir", "os.chmod",
        "os.chown", "os.truncate", "os.utime",
    } and args:
        require_inside(args[0], "sandbox filesystem changes must stay inside workspace")
    if event in {"os.rename", "os.replace", "os.link", "os.symlink"}:
        if args:
            require_inside(args[0], "sandbox filesystem changes must stay inside workspace")
        if len(args) > 1:
            require_inside(args[1], "sandbox filesystem changes must stay inside workspace")

sys.addaudithook(audit)
os.chdir(root)
sys.argv = [str(script), *script_args]
runpy.run_path(str(script), run_name="__main__")
"""


class MyDifySandboxBackend(MyDifyWorkspaceBackend, SandboxBackendProtocol):
    """Execute commands in an ephemeral copy and persist changed text files.

    Linux requires Bubblewrap and runs with mount/PID/user/network namespaces.
    Windows deliberately exposes only isolated Python script execution and
    applies a Python audit policy; it is a documented process-level fallback,
    not an OS-level security boundary.
    """

    @property
    def id(self) -> str:
        return f"my-dify:{self._account_id}:{self._app_id}:" f"{self._conversation_id}"

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        started = perf_counter()
        effective_timeout = max(1, min(timeout or 60, 300))
        if (
            not isinstance(command, str)
            or not command.strip()
            or "\x00" in command
            or len(command) > _MAX_COMMAND_LENGTH
        ):
            return self._finish(
                command=str(command),
                output="Invalid or oversized command.",
                exit_code=2,
                started=started,
            )

        with tempfile.TemporaryDirectory(prefix="my-dify-agent-") as directory:
            root = Path(directory).resolve()
            self._materialize(root)
            if os.name == "nt":
                response = self._execute_windows(
                    root,
                    command,
                    effective_timeout,
                )
            else:
                response = self._execute_linux(
                    root,
                    command,
                    effective_timeout,
                )
            self._persist_changes(root)
        return self._finish(
            command=command,
            output=response.output,
            exit_code=response.exit_code,
            started=started,
            truncated=response.truncated,
        )

    async def aexecute(
        self,
        command: str,
        *,
        timeout: int | None = None,
    ) -> ExecuteResponse:
        return await asyncio.to_thread(
            self.execute,
            command,
            timeout=timeout,
        )

    def _execute_windows(
        self,
        root: Path,
        command: str,
        timeout: int,
    ) -> ExecuteResponse:
        try:
            args = shlex.split(command, posix=False)
        except ValueError as exc:
            return ExecuteResponse(output=str(exc), exit_code=2)
        if len(args) < 2:
            return ExecuteResponse(
                output=(
                    "Windows sandbox permits only "
                    "`python <workspace-script.py> [args...]`."
                ),
                exit_code=2,
            )
        executable = Path(args[0]).name.lower()
        if executable not in {"python", "python3", "python.exe"}:
            return ExecuteResponse(
                output="Windows sandbox permits only Python workspace scripts.",
                exit_code=126,
            )
        if args[1] in {"-c", "-m", "-"}:
            return ExecuteResponse(
                output=(
                    "Inline/module execution is disabled; " "run a workspace .py file."
                ),
                exit_code=126,
            )
        script = self._resolve_script(root, args[1])
        if script is None or not script.is_file() or script.suffix != ".py":
            return ExecuteResponse(
                output="Script must be an existing .py file in this workspace.",
                exit_code=2,
            )
        return self._run_process(
            [
                sys.executable,
                "-I",
                "-c",
                _AUDIT_WRAPPER,
                str(root),
                str(script),
                *args[2:],
            ],
            root=root,
            timeout=timeout,
            preexec_fn=None,
        )

    def _execute_linux(
        self,
        root: Path,
        command: str,
        timeout: int,
    ) -> ExecuteResponse:
        bubblewrap = shutil.which("bwrap")
        if bubblewrap is None:
            return ExecuteResponse(
                output=(
                    "Secure execution is unavailable: bubblewrap is required "
                    "on Linux."
                ),
                exit_code=126,
            )
        arguments = [
            bubblewrap,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]
        for source in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(source).exists():
                arguments.extend(["--ro-bind", source, source])
        arguments.extend(
            [
                "--bind",
                str(root),
                "/workspace",
                "--chdir",
                "/workspace",
                "--setenv",
                "HOME",
                "/workspace",
                "--setenv",
                "PATH",
                "/usr/local/bin:/usr/bin:/bin",
                "/bin/sh",
                "-lc",
                command,
            ]
        )
        return self._run_process(
            arguments,
            root=root,
            timeout=timeout,
            preexec_fn=self._linux_resource_limits(timeout),
        )

    @staticmethod
    def _linux_resource_limits(timeout: int):
        def configure() -> None:
            import resource

            os.setsid()
            resource.setrlimit(
                resource.RLIMIT_CPU,
                (timeout + 1, timeout + 1),
            )
            resource.setrlimit(
                resource.RLIMIT_AS,
                (512 * 1024 * 1024, 512 * 1024 * 1024),
            )
            resource.setrlimit(
                resource.RLIMIT_FSIZE,
                (100 * 1024 * 1024, 100 * 1024 * 1024),
            )
            resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
            resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))

        return configure

    @staticmethod
    def _run_process(
        arguments: list[str],
        *,
        root: Path,
        timeout: int,
        preexec_fn,
    ) -> ExecuteResponse:
        try:
            result = subprocess.run(
                arguments,
                cwd=root,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "PYTHONIOENCODING": "utf-8",
                },
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
                check=False,
                preexec_fn=preexec_fn,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = (
                exc.stdout.decode("utf-8", "replace")
                if isinstance(
                    exc.stdout,
                    bytes,
                )
                else (exc.stdout or "")
            )
            stderr = (
                exc.stderr.decode("utf-8", "replace")
                if isinstance(
                    exc.stderr,
                    bytes,
                )
                else (exc.stderr or "")
            )
            output = (stdout + stderr).strip()
            return ExecuteResponse(
                output=f"{output}\nExecution timed out.".strip(),
                exit_code=124,
            )
        output = (result.stdout + result.stderr).strip()
        return ExecuteResponse(
            output=output[:_MAX_OUTPUT],
            exit_code=result.returncode,
            truncated=len(output) > _MAX_OUTPUT,
        )

    def _finish(
        self,
        *,
        command: str,
        output: str,
        exit_code: int,
        started: float,
        truncated: bool = False,
    ) -> ExecuteResponse:
        record = {
            "command": command,
            "exit_code": exit_code,
            "duration_ms": round((perf_counter() - started) * 1000, 3),
            "truncated": truncated,
            "output_preview": output[:2000],
            "executed_at": datetime.now(timezone.utc).isoformat(),
            "platform": "windows" if os.name == "nt" else "linux",
        }
        audit_path = (
            f"/workspace/.audit/execute-"
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-"
            f"{uuid4().hex}.json"
        )
        try:
            self.write(
                audit_path,
                json.dumps(record, ensure_ascii=False, indent=2),
            )
        except Exception:
            # Audit persistence must not mask the actual execution result.
            pass
        return ExecuteResponse(
            output=output,
            exit_code=exit_code,
            truncated=truncated,
        )

    def _materialize(self, root: Path) -> None:
        for row in self._list_paths(None):
            if row.content is None:
                continue
            virtual = self._virtual_path(row.path).lstrip("/")
            target = (root / virtual).resolve()
            if root not in target.parents:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(row.content)

    def _persist_changes(self, root: Path) -> None:
        for target in root.rglob("*"):
            if target.is_symlink() or not target.is_file():
                continue
            resolved = target.resolve()
            if root not in resolved.parents:
                continue
            relative = target.relative_to(root).as_posix()
            try:
                content = target.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            current = self.read(f"/{relative}")
            existing = (
                current.file_data["content"]
                if current.file_data is not None and not current.error
                else None
            )
            if existing != content:
                self.write(f"/{relative}", content)

    @staticmethod
    def _resolve_script(root: Path, raw: str) -> Path | None:
        normalized = raw.strip("\"'").replace("\\", "/").lstrip("/")
        if not normalized or ".." in normalized.split("/"):
            return None
        candidate = (root / normalized).resolve()
        if root not in candidate.parents:
            return None
        return candidate


__all__ = ["MyDifySandboxBackend"]
