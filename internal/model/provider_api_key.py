from sqlalchemy import (
    Column,
    UUID,
    String,
    DateTime,
    Boolean,
    text,
    PrimaryKeyConstraint,
    Index,
)
from sqlalchemy.dialects.postgresql import JSONB

from internal.extension.database_extension import db
from .account import Account


class ProviderApiKey(db.Model):
    """用户配置的模型提供商API密钥"""

    __tablename__ = "provider_api_key"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_provider_api_key_id"),
        Index("provider_api_key_account_id_idx", "account_id"),
        Index("provider_api_key_account_provider_idx", "account_id", "provider_name"),
    )

    id = Column(
        UUID, nullable=False, server_default=text("uuid_generate_v4()")
    )  # 记录id
    account_id = Column(UUID, nullable=False)  # 关联账号id
    provider_name = Column(
        String(255), nullable=False, server_default=text("''::character varying")
    )  # 提供商名称，如：tongyi、openai、moonshot等
    api_key = Column(
        String(500), nullable=False, server_default=text("''::character varying")
    )  # API密钥
    is_active = Column(
        Boolean, nullable=False, server_default=text("true")
    )  # 是否启用
    remark = Column(
        String(255), nullable=False, server_default=text("''::character varying")
    )  # 备注信息
    discovered_models = Column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )  # 最近一次显式发现的模型快照
    enabled_models = Column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )  # 用户激活使用的动态模型名字
    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(0)"),
        server_onupdate=text("CURRENT_TIMESTAMP(0)"),
    )
    created_at = Column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP(0)")
    )

    @property
    def account(self) -> "Account":
        """只读属性，返回该密钥归属的账号信息"""
        return db.session.query(Account).get(self.account_id)
