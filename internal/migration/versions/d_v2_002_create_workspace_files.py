"""create workspace_files table (R1 FilesystemMiddleware 持久化)

Revision ID: d_v2_002_create_workspace_files
Revises: d_v2_001_drop_agent_checkpoints
Create Date: 2026-07-28 19:00:00.000000

R1 改造（[deepagents-v2-plan-workspace-three-layer-paths] memory）：
- R0 没有文件持久化层，deepagents FilesystemMiddleware 默认走 LocalBackend
  （本地磁盘），多租户无法隔离 + 容器重启清零
- R1 新增 `workspace_files` 表，把 agent 的 read/write_file 落到 DB

字段说明：
- account_id / app_id / path / version → 软版本（每次写入 version+1，老行保留
  用于 audit / rollback；agent 读只取 version 最大）
- conversation_id → 当前 conversation 作用域
- storage_backend → 预留多后端（"db" 内联 / "cos" 远端），R1 只用 "db"
- content_sha256 → 内容哈希（去重 + 完整性）
- is_deleted → 软删除（30 天 GC；本迁移不删老行，creator 可见历史）

三层路径约定（详见 plan）：
  /workspace/{conversation_id}/...
  ↑ FilesystemMiddleware 路径白名单 + storage_backend.read/write 校验

不建索引的情况：
- UNIQUE (account_id, app_id, path, version) 是查询路径热点，物理存储已经
  按此排序；额外 B-Tree 重复
- content_sha256 后续接去重，R1 不建（无 dedup 逻辑时不浪费写入）
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd_v2_002_create_workspace_files'
down_revision = 'd_v2_001_drop_agent_checkpoints'
branch_labels = None
depends_on = None


def upgrade():
    """建 workspace_files 表 + conversation_id 索引。"""
    op.create_table(
        'workspace_files',
        sa.Column('id', sa.UUID(), nullable=False, server_default=sa.text('uuid_generate_v4()'), comment='主键ID'),
        sa.Column('account_id', sa.UUID(), nullable=False, comment='账号ID（多租户隔离）'),
        sa.Column('app_id', sa.UUID(), nullable=False, comment='应用ID'),
        sa.Column('conversation_id', sa.UUID(), nullable=False, comment='会话ID'),
        sa.Column('path', sa.String(length=512), nullable=False, comment="物理路径，如 '{cnv_id}/report.xlsx'"),
        sa.Column('version', sa.Integer(), nullable=False, server_default=sa.text('1'), comment='软版本号（写入时+1）'),
        sa.Column('storage_backend', sa.String(length=32), nullable=False, server_default=sa.text("'db'"), comment="存储后端：'db' 内联 / 'cos' 远端"),
        sa.Column('mime_type', sa.String(length=128), nullable=True, server_default=sa.text("'text/plain'"), comment='MIME 类型'),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False, server_default=sa.text('0'), comment='内容大小'),
        sa.Column('content_sha256', sa.String(length=64), nullable=False, server_default=sa.text("''"), comment='内容 SHA-256'),
        sa.Column('content', sa.LargeBinary(), nullable=True, comment='文件内容（storage_backend="db" 时内联）'),
        sa.Column('created_by', sa.UUID(), nullable=True, comment='创建者（account_id 或 end_user_id）'),
        sa.Column('is_deleted', sa.Boolean(), nullable=False, server_default=sa.text('false'), comment='软删除标记'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP(0)')),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP(0)')),
        sa.PrimaryKeyConstraint('id', name='pk_workspace_files'),
        sa.UniqueConstraint('account_id', 'app_id', 'path', 'version', name='uq_workspace_files_path_version'),
        sa.CheckConstraint(
            "storage_backend IN ('db', 'cos')",
            name='ck_workspace_files_storage_backend',
        ),
    )
    # 按 (account_id, app_id, path) 查最新 version 时的热点索引
    with op.batch_alter_table('workspace_files', schema=None) as batch_op:
        batch_op.create_index('idx_workspace_files_account_app_path', ['account_id', 'app_id', 'path'], unique=False)
        # end-user "本对话文件" 列表走 conversation_id 过滤
        batch_op.create_index('idx_workspace_files_conversation', ['conversation_id'], unique=False)
        # creator workspace 面板按 app 列表 + 时间排序
        batch_op.create_index('idx_workspace_files_app_created', ['app_id', 'created_at'], unique=False)


def downgrade():
    """回滚：删表（不可逆——R1 上线后线上不会跑）。"""
    with op.batch_alter_table('workspace_files', schema=None) as batch_op:
        batch_op.drop_index('idx_workspace_files_app_created')
        batch_op.drop_index('idx_workspace_files_conversation')
        batch_op.drop_index('idx_workspace_files_account_app_path')
    op.drop_table('workspace_files')
