"""WorkspaceFile 模型（R1 deepagents FilesystemMiddleware 持久化层）

详见 migration d_v2_002_create_workspace_files.py 顶部 docstring + plan
`docs/deepagents-v2-plan/00-harness-overall-upgrade.md` §1.4。

约束（硬约束 #5）：复用 SQLAlchemy + master 现有 db.Model 模式，不自建 ORM；
约束（多层路径）：路径白名单校验放在 service 层 + FilesystemBackend 双重校验，
DB 字段仅做存储，不在 schema 里约束路径格式（避免 SQLite/PG 大小写差异）。
"""
from datetime import datetime

from sqlalchemy import (
    PrimaryKeyConstraint,
    UniqueConstraint,
    CheckConstraint,
    Index,
    UUID,
    Column,
    String,
    Integer,
    DateTime,
    Boolean,
    BigInteger,
    LargeBinary,
    text,
)

from internal.extension.database_extension import db


class WorkspaceFile(db.Model):
    """workspace 文件实体（R1 FilesystemMiddleware 持久化）。

    路径约定：
      /workspace/{account_id}/{app_id}/{conversation_id|seed}/<rest>

    - conversation_id IS NULL → 路径为 `seed/...`（creator 跨会话共享）
    - conversation_id IS NOT NULL → 路径为 `{conversation_id}/...`（私有）

    软版本：`version` 每次写入 +1，老行保留（agent 读 version 最大）。
    软删除：`is_deleted=True` + 30 天 GC；creator 面板默认隐藏已删除。
    """

    __tablename__ = "workspace_files"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_workspace_files"),
        # 写路径：(account_id, app_id, path, version) UNIQUE
        UniqueConstraint(
            "account_id", "app_id", "path", "version",
            name="uq_workspace_files_path_version",
        ),
        # 读路径热点：按 (account_id, app_id, path) 找最新 version
        Index("idx_workspace_files_account_app_path", "account_id", "app_id", "path"),
        # end-user "本对话文件" 列表按 conversation_id 过滤
        Index("idx_workspace_files_conversation", "conversation_id"),
        # creator workspace 面板按 app + 时间排序
        Index("idx_workspace_files_app_created", "app_id", "created_at"),
        # storage_backend 白名单
        CheckConstraint(
            "storage_backend IN ('db', 'cos')",
            name="ck_workspace_files_storage_backend",
        ),
    )

    id = Column(UUID, server_default=text("uuid_generate_v4()"), nullable=False)
    account_id = Column(UUID, nullable=False)
    app_id = Column(UUID, nullable=False)
    conversation_id = Column(UUID, nullable=True)  # NULL 表示 seed/
    path = Column(String(512), nullable=False)
    version = Column(Integer, nullable=False, server_default=text("1"))
    storage_backend = Column(
        String(32), nullable=False, server_default=text("'db'")
    )
    mime_type = Column(
        String(128), nullable=True, server_default=text("'text/plain'")
    )
    size_bytes = Column(BigInteger, nullable=False, server_default=text("0"))
    content_sha256 = Column(
        String(64), nullable=False, server_default=text("''")
    )
    content = Column(LargeBinary, nullable=True)  # 仅 storage_backend='db' 时填充
    created_by = Column(UUID, nullable=True)
    is_deleted = Column(
        Boolean, nullable=False, server_default=text("false")
    )
    created_at = Column(
        DateTime, default=datetime.now, nullable=False
    )
    updated_at = Column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        nullable=False,
    )