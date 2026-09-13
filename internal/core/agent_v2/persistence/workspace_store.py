"""文件交付所需的持久化接口，不参与 Agent 的日常文件操作。"""

from typing import Protocol, TypedDict


class SavedWorkspaceFile(TypedDict):
    """文件目录只包含授权虚拟路径与版本元数据，不包含 COS 凭据。"""

    path: str
    revision: int
    size: int


class WorkspaceDeliveryError(RuntimeError):
    """加载或交付失败，可将消息返回给模型修正或重试。"""


class WorkspaceStore(Protocol):
    """服务端绑定账号与应用；所有路径使用 /workspace/ 下的完整虚拟路径。"""

    def list_files(self, prefix: str) -> list[SavedWorkspaceFile]:
        """只查元数据，不下载文件正文。"""
        ...

    def load_file(self, path: str) -> tuple[bytes, int]:
        """从持久化来源读取一个文件及其版本。"""
        ...

    def save_file(self, path: str, content: bytes, *, expected_revision: int) -> int:
        """显式交付文件；版本 0 仅允许新增，返回保存后的版本。"""
        ...

    def close(self) -> None:
        """释放本轮存储访问资源，不读取或提交工作副本。"""
        ...
