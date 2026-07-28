"""drop agent_checkpoints table (R0 切到 LangGraph 原生 PostgresSaver)

Revision ID: d_v2_001_drop_agent_checkpoints
Revises: 223e8bdd31a6, 6837222596d1, fe593d5bb1ab
Create Date: 2026-07-28 18:00:00.000000

R0 改造（[deepagents-v2-plan-context-design] memory）：
- master 老实现：自定义 `agent_checkpoints` 表存 deepagent-like 状态
  （thread_id / checkpoint_id / checkpoint_data JSONB / metadata JSONB）
- R0 新实现：直接复用 LangGraph 0.6.x 自带 `PostgresSaver.from_conn_string(...)`
  自动管理 `checkpoint_*` 系列表，state 由 LangGraph SDK 自己托管

为什么删而不是归档：
- 老 `agent_checkpoints` 表的 checkpoint_data 格式是 master 私有 schema
  （MessagesState 序列化），跟 LangGraph 原生 Checkpoint 二进制 ormsgpack
  不兼容，迁移过去没意义
- LangGraph 启动后老 thread_id 失效，重新跑应用走新协议即可——master
  阶段还在跑，没有存量历史 checkpoint 需要保留

依赖：
- 3 个 head 是 master 历史上同时存在的独立分支（不同 feature），本迁移
  把它们 merge 进新 chain 头（alembic 支持多 down_revision 元组）
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'd_v2_001_drop_agent_checkpoints'
down_revision = ('223e8bdd31a6', '6837222596d1', 'fe593d5bb1ab')
branch_labels = ('deepagents_v2',)
depends_on = None


def upgrade():
    """删 master 老的 `agent_checkpoints` 表。

    落地前会先建 LangGraph 原生 checkpoint_* 表（由 langgraph-checkpoint-postgres
    的 PostgresSaver.setup() 在第一次调用时自动建），所以**删老表**和
    **LangGraph 自动建表**是两个独立步骤，alembic 不参与 LangGraph 那一步。
    """
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if 'agent_checkpoints' in insp.get_table_names():
        op.drop_table('agent_checkpoints')


def downgrade():
    """回滚：重新建 agent_checkpoints 表。

    仅用于本地开发回滚；线上不会跑（生产 R0 上线后不可逆）。
    """
    op.create_table(
        'agent_checkpoints',
        sa.Column('thread_id', sa.String(length=255), nullable=False, comment='会话线程ID'),
        sa.Column('checkpoint_id', sa.String(length=255), nullable=False, comment='Checkpoint ID'),
        sa.Column('parent_checkpoint_id', sa.String(length=255), nullable=True, comment='父 Checkpoint ID'),
        sa.Column('checkpoint_data', sa.JSON(), nullable=False, comment='Checkpoint 数据'),
        sa.Column('checkpoint_metadata', sa.JSON(), nullable=True, comment='Checkpoint 元数据'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP(0)'), nullable=False, comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP(0)'), nullable=False, comment='更新时间'),
        sa.PrimaryKeyConstraint('thread_id', 'checkpoint_id', name='pk_agent_checkpoints')
    )
    with op.batch_alter_table('agent_checkpoints', schema=None) as batch_op:
        batch_op.create_index('idx_agent_checkpoints_created_at', ['created_at'], unique=False)
        batch_op.create_index('idx_agent_checkpoints_thread_id', ['thread_id'], unique=False)