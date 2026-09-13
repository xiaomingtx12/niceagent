"""Agent 工作台运行记录与持久化协议事件。"""

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UUID,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from internal.extension.database_extension import db


class AgentRun(db.Model):
    """一次 Agent 任务的状态与执行租约；暂停恢复沿用同一 Run，用量累计到 Message。"""

    __tablename__ = "agent_run"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_agent_run_id"),
        Index(
            "agent_run_conversation_source_created_idx",
            "conversation_id",
            "invoke_from",
            "created_at",
        ),
        Index(
            "agent_run_conversation_executing_uq",
            "conversation_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'cancelling')"),
        ),
        Index(
            "agent_run_conversation_input_message_uq",
            "conversation_id",
            "input_message_id",
            unique=True,
            postgresql_where=text("input_message_id IS NOT NULL"),
        ),
    )

    id = Column(UUID, nullable=False, server_default=text("uuid_generate_v4()"))
    app_id = Column(UUID, nullable=False)  # 所属应用
    conversation_id = Column(UUID, nullable=False)  # 所属会话，权限由入口服务校验
    invoke_from = Column(String(32), nullable=False)  # 入口来源，用于隔离运行查询
    message_id = Column(UUID, nullable=False)  # 承载本轮问答与用量投影的 Message.id
    input_message_id = Column(String(255), nullable=True)  # 客户端输入消息标识，在会话内用于启动幂等
    status = Column(String(32), nullable=False, server_default=text("'queued'"))  # WorkbenchRunStatus，独立于消息展示状态
    # 协议状态与表单交互信息；_interaction 保存恢复参数和提交幂等记录，图状态由 checkpoint 保存。
    state_snapshot = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    last_event_sequence = Column(Integer, nullable=False, server_default=text("0"))  # 已持久化事件游标，供流式重连续传

    # 每次启动或恢复分配新的执行租约，避免旧执行线程写入新结果。
    executor_id = Column(UUID, nullable=True)
    last_settled_executor_id = Column(UUID, nullable=True)  # 最近已结算的执行片段，与 Message 用量同事务更新
    heartbeat_at = Column(DateTime, nullable=True)  # 租约最近续期时间，用于失联恢复
    started_at = Column(DateTime, nullable=True)  # 首次开始执行的时间
    finished_at = Column(DateTime, nullable=True)  # 进入终态的时间，等待用户输入时不设置
    created_at = Column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP(0)")
    )
    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP(0)"),
        server_onupdate=text("CURRENT_TIMESTAMP(0)"),
    )


class AgentRunEvent(db.Model):
    """持久化协议事件，供流式重连续传和执行过程历史查询。"""

    __tablename__ = "agent_run_event"
    __table_args__ = (
        PrimaryKeyConstraint(
            "run_id",
            "sequence",
            name="pk_agent_run_event_run_sequence",
        ),
    )

    run_id = Column(UUID, nullable=False)  # 所属 AgentRun.id
    sequence = Column(Integer, nullable=False)  # 事件顺序与续传游标，同一 Run 内唯一
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # WorkbenchEvent 协议载荷
    created_at = Column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP(0)")
    )


__all__ = ["AgentRun", "AgentRunEvent"]
