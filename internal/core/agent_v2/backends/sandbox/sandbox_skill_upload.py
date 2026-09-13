"""压缩远程 Skill 播种流量，不改变 Agent 可见的目录或授权范围。"""

from __future__ import annotations

import shlex
import time
from io import BytesIO
from pathlib import PurePosixPath
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from deepagents.backends.protocol import SandboxBackendProtocol

from internal.core.agent_v2.backends.sandbox.sandbox_initialization import (
    SandboxProviderError,
    raise_for_file_errors,
)
from internal.core.agent_v2.observability.telemetry import AgentTelemetry, NOOP_AGENT_TELEMETRY
from internal.extension.logging_extension import logger


_MIN_COMPRESS_BYTES = 64 * 1024


def seed_sandbox_skills(
    backend: SandboxBackendProtocol, files: list[tuple[str, bytes]], *,
    projection_root: str = "/", protect: bool = False, compress: bool = False,
    telemetry: AgentTelemetry | None = None,
) -> None:
    """Skill 投影的传输与只读设置独立于工作文件，Windows 本机模式使用直接上传。"""
    if not files:
        return
    # 1.只传输已授权的包，路径验证对压缩与直接上传入口保持一致。
    _validate_skill_paths(files)
    if compress:
        upload_sandbox_skills(backend, files, projection_root=projection_root, telemetry=telemetry)
    else:
        responses = backend.upload_files(files)
        raise_for_file_errors(responses, operation="upload Sandbox Skills")
        if len(responses) != len(files):
            raise SandboxProviderError("upload Sandbox Skills: response missing")
    # 2.远程副本只读设置只作用于 Skill，临时 Workspace 始终保持可写。
    if protect:
        paths = " ".join(
            shlex.quote(str(PurePosixPath(projection_root, name)))
            for name in ("skills", "subagent_skills")
        )
        result = backend.execute(
            f'for path in {paths}; do if [ -e "$path" ]; then chmod -R a-w -- "$path"; fi; done'
        )
        if result.exit_code != 0:
            raise SandboxProviderError("failed to protect Sandbox Skill files")

# 仅使用 Python 标准库；归档内只允许普通 Skill 文件，先校验所有目标再写入。
# 文件名只通过 ZIP 传递，不拼入 Shell；finally 清理当前上传的唯一临时包。
_EXTRACT_SKILLS_SCRIPT = """import shutil, stat, sys
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

bundle = Path(sys.argv[1])
root = Path(sys.argv[2]).resolve()
try:
    with ZipFile(bundle) as archive:
        targets = []
        seen = set()
        for info in archive.infolist():
            name = PurePosixPath(info.filename)
            parts = name.parts
            if (len(parts) < 3 or parts[0] not in {'skills', 'subagent_skills'}
                    or '..' in parts or '\\\\' in info.filename
                    or info.filename != name.as_posix() or info.filename in seen
                    or stat.S_IFMT(info.external_attr >> 16) not in {0, stat.S_IFREG}):
                raise ValueError('Invalid Skill archive entry')
            seen.add(info.filename)
            target = root.joinpath(*parts)
            for candidate in (target, *target.parents):
                if candidate == root:
                    break
                if candidate.is_symlink():
                    raise ValueError('Skill destination contains a symlink')
            target.resolve().relative_to(root)
            targets.append((info, target))
        for info, target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open('wb') as destination:
                shutil.copyfileobj(source, destination)
finally:
    bundle.unlink(missing_ok=True)
"""


def upload_sandbox_skills(
    backend: SandboxBackendProtocol,
    files: list[tuple[str, bytes]],
    *,
    projection_root: str = "/",
    telemetry: AgentTelemetry | None = None,
) -> None:
    """大包压缩为一次上传；小包或压缩收益不足时保留原上传路径。"""
    if not files:
        return
    started_at = time.monotonic()
    names = _validate_skill_paths(files)
    raw_bytes = sum(len(content) for _, content in files)
    archive = _pack_skills(files, names) if raw_bytes >= _MIN_COMPRESS_BYTES else None
    if archive is not None and len(archive) <= raw_bytes * 0.9:
        upload_bytes = len(archive)
        mode = "zip"
        bundle_path = f"/tmp/nice-agent-skills-{uuid4().hex}.zip"
        responses = backend.upload_files([(bundle_path, archive)])
        raise_for_file_errors(responses, operation="upload Sandbox Skill archive")
        if len(responses) != 1:
            raise SandboxProviderError("upload Sandbox Skill archive: response missing")
        command = " ".join([
            "python3", "-c", shlex.quote(_EXTRACT_SKILLS_SCRIPT),
            shlex.quote(bundle_path), shlex.quote(projection_root),
        ])
        result = backend.execute(command)
        if result.exit_code != 0:
            raise SandboxProviderError("failed to extract Sandbox Skill archive")
    else:
        upload_bytes = raw_bytes
        mode = "direct"
        responses = backend.upload_files(files)
        raise_for_file_errors(responses, operation="upload Sandbox Skill files")
        if len(responses) != len(files):
            raise SandboxProviderError("upload Sandbox Skill files: response missing")
    duration_ms = (time.monotonic() - started_at) * 1000
    (telemetry or NOOP_AGENT_TELEMETRY).emit(
        "sandbox.skill_upload", value=duration_ms,
        attributes={
            "unit": "ms", "mode": mode, "file_count": len(files),
            "raw_bytes": raw_bytes, "upload_bytes": upload_bytes,
        },
    )
    logger.info(
        "agent_sandbox_skill_upload mode=%s files=%s raw_bytes=%s upload_bytes=%s duration_ms=%.2f",
        mode, len(files), raw_bytes, upload_bytes, duration_ms,
    )


def _validate_skill_paths(files: list[tuple[str, bytes]]) -> list[str]:
    """拒绝跨目录、重复和非规范路径，避免压缩入口扩大 Skill 写入权限。"""
    names = []
    seen = set()
    for path, _ in files:
        parsed = PurePosixPath(path)
        if (
            len(parsed.parts) < 4 or parsed.parts[0] != "/"
            or parsed.parts[1] not in {"skills", "subagent_skills"}
            or ".." in parsed.parts or "\\" in path or "\x00" in path
            or path != parsed.as_posix() or path in seen
        ):
            raise SandboxProviderError("invalid Sandbox Skill upload path")
        seen.add(path)
        names.append(path.removeprefix("/"))
    return names


def _pack_skills(files: list[tuple[str, bytes]], names: list[str]) -> bytes:
    """只归档已授权的内存内容，不重新遍历目录或读取宿主文件。"""
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for name, (_, content) in zip(names, files, strict=True):
            archive.writestr(name, content)
    return output.getvalue()
