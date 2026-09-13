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
    """会话归属与展示信息；Agent 执行上下文由 LangGraph checkpoint 保存。"""

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
    )  # 会话摘要，供任务检索使用
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
    """一轮用户提问、Agent 回答及累计用量，供历史展示与分析查询。"""

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

    # 消息关联的答案信息
    answer = Column(
        Text, nullable=False, server_default=text("''::text")
    )  # Agent生成的消息答案

    # 消息状态与执行耗时
    latency = Column(Float, nullable=False, server_default=text("0.0"))  # 各执行片段累计耗时（秒），不含等待用户输入
    is_deleted = Column(
        Boolean, nullable=False, server_default=text("false")
    )  # 软删除标记
    status = Column(
        String(255), nullable=False, server_default=text("''::character varying")
    )  # 消息的状态，涵盖正常、错误、停止
    error = Column(
        Text, nullable=False, server_default=text("''::text")
    )  # 可向用户展示的错误信息，异常堆栈写入日志

    # 累计用量，包含工具循环及暂停后恢复的模型调用
    total_token_count = Column(
        Integer, nullable=False, server_default=text("0")
    )  # 归一化后累计总 Token 数，缺少总量时按输入与输出求和
    input_token_count = Column(
        Integer, nullable=False, server_default=text("0")
    )  # 累计输入 Token 数
    output_token_count = Column(
        Integer, nullable=False, server_default=text("0")
    )  # 累计输出 Token 数
    cached_token_count = Column(
        Integer, nullable=False, server_default=text("0")
    )  # 输入 Token 中缓存命中的数量
    reasoning_token_count = Column(
        Integer, nullable=False, server_default=text("0")
    )  # 模型报告的推理 Token 数，不额外累加到总量
    model_call_count = Column(
        Integer, nullable=False, server_default=text("0")
    )  # 累计模型调用次数，包含失败或未返回用量的调用
    tool_call_count = Column(
        Integer, nullable=False, server_default=text("0")
    )  # 累计工具调用次数
    trace_id = Column(
        String(64), nullable=True
    )  # 关联运行的追踪标识；工作台链路对应 AgentRun.id
    token_usage = Column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )  # 按模型汇总的输入、输出、总 Token 及缓存/推理明细
    total_price = Column(
        Numeric(10, 7), nullable=False, server_default=text("0.0")
    )  # 历史已记录费用，分析接口仍读取；当前执行链路不计算费用

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
