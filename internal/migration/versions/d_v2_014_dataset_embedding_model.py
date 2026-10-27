"""为知识库绑定动态向量模型。"""

from alembic import op
import sqlalchemy as sa


revision = "d_v2_014_dataset_embedding"
down_revision = "05f098262b28"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增模型绑定，并保留历史通义向量集合。"""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("dataset")}
    additions = {
        "embedding_provider": sa.Column(
            "embedding_provider",
            sa.String(length=255),
            nullable=False,
            server_default="tongyi",
        ),
        "embedding_model": sa.Column(
            "embedding_model",
            sa.String(length=255),
            nullable=False,
            server_default="text-embedding-v1",
        ),
        "embedding_fingerprint": sa.Column(
            "embedding_fingerprint",
            sa.String(length=64),
            nullable=False,
            server_default="legacy_tongyi_text_embedding_v1",
        ),
        "embedding_dimension": sa.Column(
            "embedding_dimension",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    }
    for name, column in additions.items():
        if name not in columns:
            op.add_column("dataset", column)


def downgrade() -> None:
    """移除知识库向量模型绑定。"""
    for name in (
        "embedding_dimension",
        "embedding_fingerprint",
        "embedding_model",
        "embedding_provider",
    ):
        op.drop_column("dataset", name)
