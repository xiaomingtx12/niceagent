"""仅暴露本次授权 Skill 文件的只读 Backend。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import EditResult, FileUploadResponse, WriteResult


class SkillBackend(FilesystemBackend):
    """复用官方文件读取接口，独占投影目录并随运行释放。"""

    def __init__(self, files: list[tuple[str, bytes]]) -> None:
        """只复制已授权包的内容，不挂载 Catalog 或共享宿主目录。"""
        self._directory = TemporaryDirectory(prefix="nice-agent-skills-")
        root = Path(self._directory.name).resolve()
        try:
            for path, content in files:
                target = (root / path.lstrip("/")).resolve()
                if not target.is_relative_to(root) or target == root:
                    raise ValueError(f"invalid Skill projection path: {path}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            super().__init__(root_dir=root, virtual_mode=True)
        except Exception:
            self._directory.cleanup()
            raise

    def write(self, file_path: str, content: str) -> WriteResult:
        """Skill 投影不接受运行中新增或覆盖文件。"""
        return WriteResult(error="Skills are read-only")

    def edit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        """即使直接调用 Backend，也不能更改 Skill 包。"""
        return EditResult(error="Skills are read-only")

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """初始化后禁止通过批量上传绕过只读约束。"""
        return [FileUploadResponse(path=path, error="permission_denied") for path, _ in files]

    def close(self) -> None:
        """幂等释放当前 Backend 独占的临时文件。"""
        self._directory.cleanup()
