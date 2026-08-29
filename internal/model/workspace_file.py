"""Workspace 文件元数据模型。"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UUID,
    UniqueConstraint,
    text,
)

from internal.extension.database_extension import db


class WorkspaceFile(db.Model):
    """记录 Workspace 当前文件元数据，文件内容由 COS 持久化。"""

    __tablename__ = "workspace_files"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_workspace_files"),
        UniqueConstraint(
            "account_id",
            "app_id",
            "path",
            name="uq_workspace_files_scope_path",
        ),
        UniqueConstraint("object_key", name="uq_workspace_files_object_key"),
        Index("idx_workspace_files_conversation", "conversation_id"),
        Index("idx_workspace_files_app_updated", "app_id", "updated_at"),
    )

    id = Column(UUID, server_default=text("uuid_generate_v4()"), nullable=False)
    account_id = Column(UUID, nullable=False)
    app_id = Column(UUID, nullable=False)
    conversation_id = Column(UUID, nullable=False)
    path = Column(String(512), nullable=False)
    object_key = Column(String(1024), nullable=False)
    mime_type = Column(
        String(128), nullable=False, server_default=text("'application/octet-stream'")
    )
    size_bytes = Column(BigInteger, nullable=False, server_default=text("0"))
    content_sha256 = Column(String(64), nullable=False)
    revision = Column(Integer, nullable=False, server_default=text("1"))
    created_by = Column(UUID, nullable=True)
    is_deleted = Column(Boolean, nullable=False, server_default=text("false"))
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        nullable=False,
    )

    @property
    def virtual_path(self) -> str:
        """返回前端和 Agent 使用的虚拟路径。"""
        prefix = f"{self.conversation_id}/"
        relative_path = (
            self.path[len(prefix) :] if self.path.startswith(prefix) else self.path
        )
        return f"/workspace/{relative_path}"
