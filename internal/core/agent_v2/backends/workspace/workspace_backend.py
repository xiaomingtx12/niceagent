"""Agent 所需的 Workspace 能力，不依赖具体数据库和对象存储。"""

from abc import abstractmethod

from deepagents.backends.protocol import BackendProtocol, FileUploadResponse

from internal.core.agent_v2.entity.workspace_snapshot import WorkspaceManifest


class WorkspaceBackend(BackendProtocol):
    """在文件 Backend 基础上提供版本化同步和资源释放契约。"""

    @abstractmethod
    def export_snapshot(self) -> tuple[WorkspaceManifest, list[tuple[str, bytes]]]:
        """从一致的版本记录导出文件内容，作为 Sandbox 同步基线。"""
        raise NotImplementedError

    @abstractmethod
    def upload_files(
        self,
        files: list[tuple[str, bytes]],
        *,
        expected_revisions: dict[str, int] | None = None,
    ) -> list[FileUploadResponse]:
        """按预期版本写入文件，冲突通过逐文件结果返回。"""
        raise NotImplementedError

    @abstractmethod
    def delete_files(
        self,
        paths: list[str],
        *,
        expected_revisions: dict[str, int] | None = None,
    ) -> list[str]:
        """删除指定版本的文件，返回未成功删除的原因。"""
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        """释放本次运行独占的资源，可重复调用。"""
        raise NotImplementedError
