"""Agent 工作台运行记录与持久化协议事件。"""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import (
    Column,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UUID,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from internal.extension.database_extension import db
from internal.entity.agent_run_entity import AGENT_RUN_TERMINAL_STATUSES

_INTERACTION_VALUE_KEYS = ("pending_inputs", "input_history", "input_recovery")


class AgentRun(db.Model):
    """一次用户任务及当前执行租约；暂停恢复沿用任务身份，用量累计到 Message。"""

    __tablename__ = "agent_run"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_agent_run_id"),
        UniqueConstraint("message_id", name="uq_agent_run_message_id"),
        CheckConstraint(
            "status IN ('running', 'waiting_input', 'completed', 'failed', 'cancelled')",
            name="ck_agent_run_status",
        ),
        CheckConstraint("last_event_sequence >= 0", name="ck_agent_run_event_sequence"),
        # 执行权只在运行中存在：运行必须有执行标识，其余状态不得保留旧执行段的写入资格。
        CheckConstraint(
            "(status = 'running' AND executor_id IS NOT NULL) OR "
            "(status IN ('waiting_input', 'completed', 'failed', 'cancelled') "
            "AND executor_id IS NULL)",
            name="ck_agent_run_executor",
        ),
        CheckConstraint(
            "(status IN ('completed', 'failed', 'cancelled') AND finished_at IS NOT NULL) OR "
            "(status IN ('running', 'waiting_input') AND finished_at IS NULL)",
            name="ck_agent_run_finished_at",
        ),
        CheckConstraint(
            "(input_message_id IS NULL OR input_message_id <> '')",
            name="ck_agent_run_input_message_nonempty",
        ),
        CheckConstraint(
            "(finished_at IS NULL OR started_at <= finished_at)",
            name="ck_agent_run_started_before_finished",
        ),
        CheckConstraint(
            "jsonb_typeof(invocation_snapshot) = 'object'",
            name="ck_agent_run_invocation_snapshot_object",
        ),
        CheckConstraint(
            "jsonb_typeof(interaction_state) = 'object'",
            name="ck_agent_run_interaction_state_object",
        ),
        CheckConstraint(
            "jsonb_typeof(state_snapshot) = 'object'",
            name="ck_agent_run_state_snapshot_object",
        ),
        # 会话内按时间线读取历史、最新 Run 和状态，入口隔离是会话内的低基数过滤。
        Index(
            "agent_run_conversation_created_idx",
            "conversation_id",
            "created_at",
            "id",
        ),
        # 会话占用同时承担互斥和失联收敛两个入口：收敛只按“仍在占用”这一个条件扫描。
        Index(
            "agent_run_conversation_occupied_uq",
            "conversation_id",
            unique=True,
            postgresql_where=text("status IN ('running', 'waiting_input')"),
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
    app_id = Column(UUID, nullable=False)  # 所属应用标识；内置助手不要求存在 App 记录；必须与 message.app_id 一致
    conversation_id = Column(UUID, nullable=False)  # 所属会话，权限由入口服务校验；必须与 message.conversation_id 一致
    invoke_from = Column(String(32), nullable=False)  # 入口来源，用于隔离运行查询
    message_id = Column(
        UUID, ForeignKey("message.id", ondelete="CASCADE"), nullable=False
    )  # 承载本轮问答与用量投影的 Message.id；归属三元组一致性由创建服务保证
    input_message_id = Column(String(255), nullable=True)  # 对齐客户端消息、历史和时间线；相同标识重复启动时复用 Run
    status = Column(
        String(32), nullable=False, server_default=text("'running'")
    )  # AgentRunStatus，独立于消息展示状态；建行即已分配执行权
    error_message = Column(
        Text, nullable=False, server_default=text("''::text")
    )  # 可向用户展示的 Run 失败原因；异常堆栈只写入日志
    invocation_snapshot = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # 补充表单后恢复原调用参数，校验配置指纹
    interaction_state = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # 待填表单、已确认输入及取消后重新填写的状态
    # 只保存展示投影；调用参数、交互规则和图 checkpoint 不写入此字段。
    state_snapshot = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    last_event_sequence = Column(Integer, nullable=False, server_default=text("0"))  # 已持久化事件游标，供流式重连续传

    executor_id = Column(UUID, nullable=True)  # 本次执行段的标识，不是账号或进程 ID；启动/恢复换新，结算清空
    cancel_requested_at = Column(
        DateTime, nullable=True
    )  # 收到取消请求的时间；执行线程与状态查询都读它，终态保留原值供追溯
    started_at = Column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP(0)")
    )  # 首次开始执行的时间；建行即赋值，暂停恢复不改
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

    @property
    def is_terminal(self) -> bool:
        """统一记录的终态判断，等待用户输入仍可继续执行。"""
        return self.status in AGENT_RUN_TERMINAL_STATUSES

    @property
    def public_snapshot(self) -> dict[str, Any]:
        """合并公开投影，调用方无需了解交互字段的存储位置。"""
        interaction = self.interaction_state or {}
        return {
            **(self.state_snapshot or {}),
            **{key: interaction[key] for key in _INTERACTION_VALUE_KEYS if key in interaction},
        }

    def apply_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        """整体赋值以触发 JSONB 更新，公开事件不能覆盖私有恢复参数。"""
        public = {key: value for key, value in snapshot.items() if not key.startswith("_")}
        self.state_snapshot = {
            **(self.state_snapshot or {}),
            **{key: value for key, value in public.items() if key not in _INTERACTION_VALUE_KEYS},
        }
        interaction = {key: public[key] for key in _INTERACTION_VALUE_KEYS if key in public}
        if interaction:
            self.interaction_state = {**(self.interaction_state or {}), **interaction}


class AgentRunSubmission(db.Model):
    """每批表单仅接受一份答案；主键由中断 ID 集合决定，与答案内容无关。"""

    __tablename__ = "agent_run_submission"
    __table_args__ = (
        PrimaryKeyConstraint("run_id", "input_batch_id", name="pk_agent_run_submission_batch"),
        CheckConstraint("accepted_sequence >= 0", name="ck_agent_run_submission_sequence"),
        CheckConstraint(
            "jsonb_typeof(answers) = 'object'",
            name="ck_agent_run_submission_answers_object",
        ),
    )

    run_id = Column(UUID, ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=False)
    input_batch_id = Column(String(64), nullable=False)  # 整批中断 ID 集合的摘要；不是答案哈希或用户 ID
    answers = Column(JSONB, nullable=False)
    accepted_sequence = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP AT TIME ZONE 'UTC'"))


class AgentRunEvent(db.Model):
    """持久化协议事件，供流式重连续传和执行过程历史查询。"""

    __tablename__ = "agent_run_event"
    __table_args__ = (
        PrimaryKeyConstraint(
            "run_id",
            "sequence",
            name="pk_agent_run_event_run_sequence",
        ),
        CheckConstraint("sequence > 0", name="ck_agent_run_event_sequence_positive"),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_agent_run_event_payload_object",
        ),
    )

    run_id = Column(UUID, ForeignKey("agent_run.id", ondelete="CASCADE"), nullable=False)
    sequence = Column(Integer, nullable=False)  # 事件顺序与续传游标，同一 Run 内唯一
    payload = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # WorkbenchEvent 协议载荷
    created_at = Column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP AT TIME ZONE 'UTC'")
    )


__all__ = ["AgentRun", "AgentRunSubmission", "AgentRunEvent"]
