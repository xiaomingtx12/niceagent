"""合并 Agent Usage 迁移与 LangGraph 保留表迁移分支。"""

from alembic import op


revision = "d_v2_011_merge_agent_usage_heads"
down_revision = (
    "d_v2_010_agent_usage_fields",
    "f558918e9bc3",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    """合并两个迁移分支，不执行额外数据库操作。"""


def downgrade() -> None:
    """回滚时恢复两个独立迁移分支。"""
