"""本轮临时工作副本；文件能力直接复用官方 FilesystemBackend。"""

from pathlib import Path
from tempfile import TemporaryDirectory

from deepagents.backends import FilesystemBackend


class WorkspaceProjection:
    """持有临时目录，供无 Sandbox 模式使用，或作为远程 Sandbox 启动前的工作副本。"""

    def __init__(self) -> None:
        """创建空副本，不读取文件目录、数据库或 COS。"""
        self._directory = TemporaryDirectory(prefix="my-dify-workspace-")
        try:
            self.root = Path(self._directory.name).resolve()
            workspace_root = self.root / "workspace"
            workspace_root.mkdir()
            self.backend = FilesystemBackend(root_dir=workspace_root, virtual_mode=True)
        except BaseException:
            self._directory.cleanup()
            raise

    def files(self) -> list[tuple[str, bytes]]:
        """仅迁移本轮已加载或生成的本地文件，远程启动不下载整个持久化文件库。"""
        # 1.枚举本轮目录，拒绝符号链接，避免迁移时越出工作副本。
        root = self.root / "workspace"
        files = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise ValueError("Workspace projection must not contain symbolic links")
            if path.is_file():
                files.append((f"/workspace/{path.relative_to(root).as_posix()}", path.read_bytes()))
        return files

    def close(self) -> None:
        """只删除临时副本；副本内的删除和修改不会传播到 COS。"""
        self._directory.cleanup()
