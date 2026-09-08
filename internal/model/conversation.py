from sqlalchemy import (
    Column,
    UUID,
    String,
    Text,
    Integer,
    DateTime,
    Boolean,
    Numeric,
    Float,
    text,
    PrimaryKeyConstraint,
    func,
    Index,
)
from sqlalchemy.dialects.postgresql import JSONB
from internal.extension.database_extension import db


class Conversation(db.Model):
    """交流会话模型"""

    __tablename__ = "conversation"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_conversation_id"),
        Index("conversation_app_id_idx", "app_id"),
        Index("conversation_app_created_by_idx", "created_by"),
        Index("conversation_owner_account_idx", "owner_account_id"),
    )

    id = Column(UUID, nullable=False, server_default=text("uuid_generate_v4()"))
    app_id = Column(UUID, nullable=False)  # 关联应用id
    name = Column(
        String(255), nullable=False, server_default=text("''::character varying")
    )  # 会话名称
    summary = Column(
        Text, nullable=False, server_default=text("''::text")
    )  # 会话摘要/长期记忆
    is_pinned = Column(
        Boolean, nullable=False, server_default=text("false")
    )  # 是否置顶
    is_deleted = Column(
        Boolean, nullable=False, server_default=text("false")
    )  # 是否删除
    invoke_from = Column(
        String(255), nullable=False, server_default=text("''::character varying")
    )  # 调用来源
    created_by = Column(
        UUID,
        nullable=True,
    )  # 会话来源主体；按入口记录 Account.id 或 EndUser.id，不作为平台账号鉴权依据
    owner_account_id = Column(
        UUID,
        nullable=True,
    )  # 平台账号维度的稳定会话归属；外部渠道没有登录账号时允许为空
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
    def is_new(self) -> bool:
        """只读属性，用于判断该会话是否是第一次创建"""
        message_count = (
            db.session.query(func.count(Message.id))
            .filter(
                Message.conversation_id == self.id,
            )
            .scalar()
        )

        return False if message_count > 1 else True


class Message(db.Model):
    """交流消息模型"""

    __tablename__ = "message"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_message_id"),
        Index(
            "message_conversation_created_id_idx",
            "conversation_id",
            "created_at",
            "id",
            postgresql_where=text("is_deleted = false"),
        ),
        Index("message_created_by_idx", "created_by"),
    )

    id = Column(UUID, nullable=False, server_default=text("uuid_generate_v4()"))

    # 消息关联的记录
    app_id = Column(UUID, nullable=False)  # 关联应用id
    conversation_id = Column(UUID, nullable=False)  # 关联会话id
    invoke_from = Column(
        String(255),
        nullable=False,
        server_default=text("''::character varying"),
    )  # 调用来源，涵盖service_api、web_app、debugger等
    created_by = Column(
        UUID, nullable=False
    )  # 消息来源主体，可能是 Account.id 或 EndUser.id，权限继承所属会话

    # 消息关联的原始问题
    query = Column(
        Text, nullable=False, server_default=text("''::text")
    )  # 用户提问的原始query
    image_urls = Column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )  # 用户提问的图片URL列表信息
    attachments = Column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )  # 用户提问时提交的附件元数据
    message = Column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )  # 产生answer的消息列表
    message_token_count = Column(
        Integer, nullable=False, server_default=text("0")
    )  # 消息列表的token总数
    message_unit_price = Column(
        Numeric(10, 7), nullable=False, server_default=text("0.0")
    )  # 消息的单价
    message_price_unit = Column(
        Numeric(10, 4), nullable=False, server_default=text("0.0")
    )  # 消息的价格单位

    # 消息关联的答案信息
    answer = Column(
        Text, nullable=False, server_default=text("''::text")
    )  # Agent生成的消息答案
    answer_token_count = Column(
        Integer, nullable=False, server_default=text("0")
    )  # 消息答案的token数
    answer_unit_price = Column(
        Numeric(10, 7), nullable=False, server_default=text("0.0")
    )  # token的单位价格
    answer_price_unit = Column(
        Numeric(10, 4), nullable=False, server_default=text("0.0")
    )  # token的价格单位

    # 消息的相关统计信息
    latency = Column(Float, nullable=False, server_default=text("0.0"))  # 消息的总耗时
    is_deleted = Column(
        Boolean, nullable=False, server_default=text("false")
    )  # 软删除标记
    status = Column(
        String(255), nullable=False, server_default=text("''::character varying")
    )  # 消息的状态，涵盖正常、错误、停止
    error = Column(
        Text, nullable=False, server_default=text("''::text")
    )  # 发生错误时记录的信息
    total_token_count = Column(
        Integer, nullable=False, server_default=text("0")
    )  # 消耗的总token数，计算步骤的消耗
    input_token_count = Column(
        Integer, nullable=False, server_default=text("0")
    )  # 本次运行输入Token总数
    output_token_count = Column(
        Integer, nullable=False, server_default=text("0")
    )  # 本次运行输出Token总数
    cached_token_count = Column(
        Integer, nullable=False, server_default=text("0")
    )  # 本次运行缓存命中的输入Token数
    reasoning_token_count = Column(
        Integer, nullable=False, server_default=text("0")
    )  # 本次运行推理Token数
    model_call_count = Column(
        Integer, nullable=False, server_default=text("0")
    )  # 本次运行真实模型调用次数
    tool_call_count = Column(
        Integer, nullable=False, server_default=text("0")
    )  # 本次运行真实工具调用次数
    trace_id = Column(
        String(64), nullable=True
    )  # 本次运行的Trace标识
    token_usage = Column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )  # 按模型保存的Token明细
    total_price = Column(
        Numeric(10, 7), nullable=False, server_default=text("0.0")
    )  # 消耗的总价格，计算步骤的总消耗

    # 消息时间相关信息
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
    def conversation(self) -> Conversation:
        """只读属性，返回该消息对应的会话记录"""
        return db.session.query(Conversation).get(self.conversation_id)
