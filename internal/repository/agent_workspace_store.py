"""把已授权 Workspace 仓储和 COS 接入显式文件交付接口。"""

import mimetypes
from hashlib import sha256

from internal.core.agent_v2.entity.workspace_path import AgentWorkspacePathPolicy
from internal.core.agent_v2.entity.workspace_quota import WorkspaceQuotaExceeded
from internal.core.agent_v2.observability.telemetry import AgentTelemetry
from internal.core.agent_v2.persistence.workspace_store import SavedWorkspaceFile, WorkspaceDeliveryError
from internal.repository.workspace_file_repository import (
    WorkspaceFileRepository,
    WorkspaceFileRevisionConflict,
    WorkspaceStorageError,
)
from internal.repository.workspace_file_store import WorkspaceFileStore


class AgentWorkspaceStore:
    """只负责持久化文件的目录、按需读取和交付，日常文件工具使用工作副本。"""

    def __init__(
        self, *, repository: WorkspaceFileRepository, file_store: WorkspaceFileStore,
        telemetry: AgentTelemetry,
    ) -> None:
        """身份和配额由装配服务绑定，存储操作不接受模型提供账号。"""
        self._repository = repository
        self._file_store = file_store
        self._telemetry = telemetry
        self._paths = AgentWorkspacePathPolicy(scope_id=repository.scope_id)

    def list_files(self, prefix: str) -> list[SavedWorkspaceFile]:
        """列目录仅查询 SQL 元数据，不扫描 COS 对象或下载正文。"""
        storage_prefix = self._paths.resolve_prefix(prefix)
        return [
            {"path": self._paths.virtual_path(row.path), "revision": row.revision, "size": row.size_bytes}
            for row in self._repository.list((storage_prefix,))
        ]

    def load_file(self, path: str) -> tuple[bytes, int]:
        """仅下载指定文件，并校验读取内容与记录版本相符。"""
        # 1.按服务端作用域查找文件，COS key 不由调用者传入。
        row = self._repository.fetch(self._paths.resolve_file(path))
        if row is None:
            raise WorkspaceDeliveryError("已保存文件不存在，请先查看文件目录")
        try:
            content = self._file_store.read_bytes(row)
        except WorkspaceStorageError as exc:
            raise WorkspaceDeliveryError("文件内容读取失败，请稍后重试") from exc
        # 2.把实际加载版本交给交付流程，后续覆盖不得跳过版本检查。
        if sha256(content).hexdigest() != row.content_sha256:
            raise WorkspaceDeliveryError("文件内容与版本记录不一致，请重新加载")
        return content, row.revision

    def save_file(self, path: str, content: bytes, *, expected_revision: int) -> int:
        """只有交付入口可以提交 COS；版本冲突和配额失败返回稳定业务错误。"""
        storage_path = self._paths.resolve_file(path)
        mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        try:
            row = self._file_store.write_bytes(
                self._repository, storage_path, content, mime_type=mime_type,
                expected_revision=expected_revision, telemetry=self._telemetry,
            )
        except WorkspaceFileRevisionConflict as exc:
            raise WorkspaceDeliveryError(
                "目标文件已存在或版本已更新；请加载最新文件后重新编辑，或使用新的交付路径"
            ) from exc
        except WorkspaceQuotaExceeded as exc:
            raise WorkspaceDeliveryError("文件交付超出工作区配额，请减少文件大小或数量") from exc
        except WorkspaceStorageError as exc:
            raise WorkspaceDeliveryError("文件保存失败，本轮工作副本仍可用于重试交付") from exc
        return row.revision

    def close(self) -> None:
        """释放独立仓储会话，不进行文件同步或删除。"""
        self._repository.close()
