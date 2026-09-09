"""Skill 压缩传输的 Provider 接线、字节预算和远端解包回归测试。"""

import random
import shlex
import subprocess
import sys
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile, ZipInfo

import pytest

from deepagents.backends.protocol import ExecuteResponse, FileUploadResponse

from internal.core.agent_v2.backends.sandbox import daytona_sandbox_backend
from internal.core.agent_v2.backends.sandbox.sandbox_skill_upload import upload_sandbox_skills
from internal.core.agent_v2.backends.sandbox.sandbox_workspace_sync import (
    SandboxProviderError,
    seed_sandbox_workspace,
)


def test_daytona_initialization_compresses_large_skill_payload(monkeypatch):
    """驱动生产初始化入口，防止大 Skill 包重新变成原始全文上传。"""
    uploads = []
    commands = []
    files = [("/skills/demo/data.txt", b"sample skill reference\n" * 12000)]

    class Backend:
        """替代远程传输，但保留工厂的真实播种流程。"""

        def __init__(self, **kwargs):
            """接受 Provider 生命周期的构造参数。"""

        def upload_files(self, batch):
            """捕获真正发给 Provider 的字节。"""
            uploads.extend(batch)
            return [FileUploadResponse(path=path) for path, _ in batch]

        def execute(self, command, **kwargs):
            """记录目录创建和解包命令。"""
            commands.append(command)
            return ExecuteResponse(output="", exit_code=0)

        def set_workspace_baseline(self, manifest):
            """测试场景没有历史文件，播种结果必须保留空基线。"""
            assert manifest == {}

    monkeypatch.setattr(daytona_sandbox_backend, "ManagedDaytonaSandbox", Backend)
    monkeypatch.setattr(daytona_sandbox_backend, "Daytona", lambda _: SimpleNamespace(create=lambda: object()))
    daytona_sandbox_backend.create_backend(
        settings=SimpleNamespace(), sandbox_name="test", skill_files=files,
        workspace=SimpleNamespace(export_snapshot=lambda: ({}, [])),
    )
    assert sum(len(content) for _, content in uploads) < len(files[0][1]) / 2
    assert len(uploads) == 1
    assert sum(command.startswith("mkdir -p") for command in commands) == 1
    assert "chmod -R a-w" in commands[-1]


class _LocalTransfer:
    """将远程协议映射到临时目录，实际执行生产生成的解包脚本。"""

    def __init__(self, root, *, daytona=False):
        """保留路径转换和传输记录，检查两种 Provider 的物理路径契约。"""
        self.root = root
        self.daytona = daytona
        self.uploads = []
        self.commands = []

    def upload_files(self, files):
        """在虚拟上传目标保存完整字节，不模拟解包后的文件内容。"""
        self.uploads.extend(files)
        for path, content in files:
            if self.daytona:
                path = daytona_sandbox_backend._translate_virtual_path(path)
            target = self.root / path.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        return [FileUploadResponse(path=path) for path, _ in files]

    def execute(self, command):
        """Shell 参数解析后替换执行器与根目录，脚本内容保持原样。"""
        if self.daytona:
            command = daytona_sandbox_backend._translate_virtual_paths(command)
        self.commands.append(command)
        args = shlex.split(command)
        assert args[:2] == ["python3", "-c"]
        args[0] = sys.executable
        args[-2:] = [str(self.root / value.lstrip("/")) for value in args[-2:]]
        result = subprocess.run(args, capture_output=True, text=True, timeout=10)
        return ExecuteResponse(output=result.stdout + result.stderr, exit_code=result.returncode)


@pytest.mark.parametrize("daytona", [False, True])
def test_compressed_skills_are_extracted_exactly_and_archive_is_removed(tmp_path, daytona):
    """用真实解包验证特殊文件名、二进制内容和子 Agent 文件不会损坏或串目录。"""
    backend = _LocalTransfer(tmp_path, daytona=daytona)
    projection_root = "/home/daytona" if daytona else "/"
    files = [
        ("/skills/中文技能/带空格 ' $文件.md", "技能内容\n".encode() * 12000),
        ("/subagent_skills/worker/demo/data.bin", bytes(range(256)) * 400),
    ]
    upload_sandbox_skills(backend, files, projection_root=projection_root)
    assert len(backend.uploads) == 1
    assert len(backend.commands) == 1
    assert sum(len(data) for _, data in backend.uploads) < sum(len(data) for _, data in files) / 2
    for path, expected in files:
        actual = tmp_path / projection_root.lstrip("/") / path.lstrip("/")
        assert actual.read_bytes() == expected
    assert not list(tmp_path.rglob("*.zip"))


@pytest.mark.parametrize("large", [False, True], ids=["small", "incompressible"])
def test_small_or_incompressible_skills_use_direct_upload(tmp_path, large):
    """小包与不可压缩内容不增加远程解包请求。"""
    backend = _LocalTransfer(tmp_path)
    content = random.Random(0).randbytes(100000) if large else b"small skill"
    files = [("/skills/demo/data.bin", content)]
    upload_sandbox_skills(backend, files)
    assert backend.uploads == files
    assert backend.commands == []
    assert (tmp_path / "skills/demo/data.bin").read_bytes() == content


@pytest.mark.parametrize("path", [
    "/workspace/escape.txt", "/skills/demo/../../escape.txt", "/skills/demo/./x",
    "/skills/demo//x", "/skills/demo/x\\y", "skills/demo/x", "/skills/demo/x\x00",
])
def test_invalid_skill_paths_are_rejected_before_upload(tmp_path, path):
    """上传边界必须在任何远程操作前拒绝越权路径。"""
    backend = _LocalTransfer(tmp_path)
    with pytest.raises(SandboxProviderError, match="invalid"):
        upload_sandbox_skills(backend, [(path, b"data")])
    assert backend.uploads == []


@pytest.mark.parametrize("failure", ["missing_response", "upload_error", "extract_error"])
def test_failed_skill_transfer_never_sets_workspace_baseline(failure):
    """播种不完整时不能标记成功，避免关闭时把部分沙箱文件回写为权威数据。"""
    baselines = []

    def upload(files):
        """分别模拟 SDK 缺失结果与显式上传失败。"""
        if failure == "missing_response":
            return []
        return [FileUploadResponse(
            path=files[0][0], error="permission_denied" if failure == "upload_error" else None,
        )]

    backend = SimpleNamespace(
        upload_files=upload,
        execute=lambda _: ExecuteResponse(output="failed", exit_code=1),
        set_workspace_baseline=baselines.append,
    )
    with pytest.raises(SandboxProviderError):
        seed_sandbox_workspace(
            backend, workspace=SimpleNamespace(export_snapshot=lambda: ({}, [])),
            skill_files=[("/skills/demo/data.txt", b"compressible" * 12000)],
            skill_uploader=lambda files: upload_sandbox_skills(backend, files),
        )
    assert baselines == []


@pytest.mark.parametrize("entry", ["../escape.txt", "workspace/escape.txt", "symlink"])
def test_remote_extractor_rejects_unsafe_entries_before_writing(tmp_path, monkeypatch, entry):
    """即使上传包内容被替换，远端也应先拒绝不安全条目并清理临时包。"""
    backend = _LocalTransfer(tmp_path)
    original_upload = backend.upload_files

    def replace_archive(files):
        """构造含合法前缀文件的坏包，验证校验完成前不会写入任何条目。"""
        data = BytesIO()
        with ZipFile(data, "w") as archive:
            archive.writestr("skills/demo/valid.txt", b"must not be written")
            if entry == "symlink":
                info = ZipInfo("skills/demo/link")
                info.external_attr = 0o120777 << 16
                archive.writestr(info, b"../../outside")
            else:
                archive.writestr(entry, b"outside")
        return original_upload([(files[0][0], data.getvalue())])

    monkeypatch.setattr(backend, "upload_files", replace_archive)
    with pytest.raises(SandboxProviderError, match="extract"):
        upload_sandbox_skills(backend, [("/skills/demo/valid.txt", b"compress" * 12000)])
    assert not (tmp_path / "skills").exists()
    assert not list(tmp_path.rglob("*.zip"))
