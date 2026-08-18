"""create memory_facts table (R3 用户结构化事实)

Revision ID: d_v2_003_create_memory_facts
Revises: d_v2_002_create_workspace_files
Create Date: 2026-07-28 20:00:00.000000

R3 改造（[deepagents-v2-plan-user-memory-visibility] memory）：
- 旧的对话级摘要只服务上下文压缩，end-user 看不到、不能管理、不能删除
- R3 引入 `memory_facts` 表存 LLM 自动抽取的结构化事实（不是摘要）
  - fact 内容短（一句话），结构稳定
  - 用于跨 session 生成只读 Memory Profile，并通过 DeepAgents memory= 投影
- 三类记忆分离（详见 plan §2.2）：
  - 压缩 (SummarizationMiddleware, 不落库)
  - 原始对话 (conversation/message 表, 冷启动和审计)
  - fact (本表, R3)

字段说明：
- account_id / app_id → 业务范围（事实归属哪个 app / 账号）
- end_user_id → NULL 表示 creator / account 级事实；非 NULL 表示端用户事实
- fact_type → 结构化类别 ('preference' / 'profile' / 'instruction' 等)
  - R3 不强制枚举，存 str 即可（业务按需）
- content → 事实正文（短文本，1-2 句）
- confidence → LLM 抽取可信度 (0.0-1.0)，供召回排序
- source_conversation_id → 抽取来源会话（audit / 回滚到该对话）
- auto_extracted → True 表示 LLM 自动抽取；False 表示用户/creator 手动写入
- deleted_at → 软删除时间；NULL 表示未删除

可见性矩阵（plan §3.1）：
- account 级（end_user_id IS NULL）：creator 全权管理
- end-user 级（end_user_id IS NOT NULL）：
  - end-user 在 WebApp "我的记忆" 页面可见本人 fact
  - creator 可见 app 下所有 fact（用于内容审核 / 调试）
  - 跨 account 不可见（强隔离）

索引策略：
- end-user "我的记忆" 列表：(account_id, app_id, end_user_id) 过滤 + 时间排序
- creator 面板：(account_id, app_id) + 时间排序（不分 end_user_id）
- 召回排序：confidence + updated_at
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd_v2_003_create_memory_facts'
down_revision = 'd_v2_002_create_workspace_files'
branch_labels = None
depends_on = None


def upgrade():
    """建 memory_facts 表 + 索引."""
    # 旧版 Skill/V2 链已经创建过一张同名表，但字段模型完全不同。
    # 后续 d_v2_006 只做兼容收敛，d_v2_009 会删除这张废弃表，因此
    # 这里不能因为同名表存在而阻断从 b8c9d0e1f2a3 升级的数据库。
    bind = op.get_bind()
    if "memory_facts" in sa.inspect(bind).get_table_names():
        return

    op.create_table(
        'memory_facts',
        sa.Column('id', sa.UUID(), nullable=False, server_default=sa.text('uuid_generate_v4()'), comment='主键ID'),
        sa.Column('account_id', sa.UUID(), nullable=False, comment='账号ID（多租户隔离）'),
        sa.Column('app_id', sa.UUID(), nullable=False, comment='应用ID'),
        sa.Column('end_user_id', sa.UUID(), nullable=True, comment='端用户ID（NULL 表示 account 级 / creator fact）'),
        sa.Column('fact_type', sa.String(length=64), nullable=False, comment="事实类别: 'preference' / 'profile' / 'instruction' / 等"),
        sa.Column('content', sa.Text(), nullable=False, comment='事实正文（短文本，1-2 句）'),
        sa.Column('confidence', sa.Float(), nullable=False, server_default=sa.text('1.0'), comment='LLM 抽取可信度 (0.0-1.0)'),
        sa.Column('source_conversation_id', sa.UUID(), nullable=True, comment='抽取来源会话（audit / 回溯）'),
        sa.Column('auto_extracted', sa.Boolean(), nullable=False, server_default=sa.text('true'), comment='True=LLM 自动抽取；False=用户/creator 手动写入'),
        sa.Column('deleted_at', sa.DateTime(), nullable=True, comment='软删除时间；NULL=未删除'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP(0)')),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP(0)')),
        sa.PrimaryKeyConstraint('id', name='pk_memory_facts'),
        # confidence 必须 0.0-1.0
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name='ck_memory_facts_confidence_range',
        ),
    )
    with op.batch_alter_table('memory_facts', schema=None) as batch_op:
        # end-user "我的记忆" 列表 (account_id, app_id, end_user_id) + 时间排序
        batch_op.create_index(
            'idx_memory_facts_end_user', ['account_id', 'app_id', 'end_user_id'],
            unique=False,
        )
        # creator 面板 (account_id, app_id) + 时间排序
        batch_op.create_index(
            'idx_memory_facts_app_updated', ['account_id', 'app_id', 'updated_at'],
            unique=False,
        )
        # 召回排序: confidence + updated_at
        batch_op.create_index(
            'idx_memory_facts_recall', ['account_id', 'app_id', 'confidence', 'updated_at'],
            unique=False,
        )
        # end_user_id 用于快速过滤未删除 end-user fact
        batch_op.create_index(
            'idx_memory_facts_deleted_at', ['deleted_at'],
            unique=False,
        )


def downgrade():
    """回滚: 删表 (R3 上线后线上不会跑)."""
    with op.batch_alter_table('memory_facts', schema=None) as batch_op:
        batch_op.drop_index('idx_memory_facts_deleted_at')
        batch_op.drop_index('idx_memory_facts_recall')
        batch_op.drop_index('idx_memory_facts_app_updated')
        batch_op.drop_index('idx_memory_facts_end_user')
    op.drop_table('memory_facts')
