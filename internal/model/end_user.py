from sqlalchemy import Column, UUID, DateTime, text, PrimaryKeyConstraint, Index

from internal.extension.database_extension import db


class EndUser(db.Model):
    """应用内使用者身份；可关联登录账号，应用所有者由 App.account_id 确定。"""

    __tablename__ = "end_user"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_end_user_id"),
        Index("end_user_app_id_idx", "app_id"),
        Index(
            "uq_end_user_app_account",
            "app_id",
            "account_id",
            unique=True,
        ),
    )

    id = Column(
        UUID, nullable=False, server_default=text("uuid_generate_v4()")
    )  # 应用用户记录主键；会话来源和 end_user_id 引用此值
    app_id = Column(UUID, nullable=False)  # App.id；用户身份只在该应用内有效
    account_id = Column(
        UUID, nullable=True
    )  # 使用者的 Account.id；WebApp 由登录身份确定，OpenAPI/微信未绑定账号时为空
    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(0)"),
        server_onupdate=text("CURRENT_TIMESTAMP(0)"),
    )
    created_at = Column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP(0)")
    )
