"""本轮执行、表单接受事实及持久化协议事件。"""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import (
    Column,
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
from internal.entity.agent_execution_entity import AGENT_EXECUTION_TERMINAL_STATUSES

_DISPLAY_STATE_KEYS = ("messages", "todos", "files", "async_tasks")


class AgentExecution(db.Model):
    """本轮逻辑执行与写入资格；表单恢复沿用 Execution，停止后只能开始新一轮。"""

    __tablename__ = "agent_execution"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_agent_execution_id"),
        UniqueConstraint("turn_id", name="uq_agent_execution_turn_id"),
        # 会话内按时间线读取历史、最新 Run 和状态，入口隔离是会话内的低基数过滤。
        Index(
            "agent_execution_conversation_created_idx",
            "conversation_id",
            "created_at",
            "id",
        ),
        # 会话占用同时承担互斥和失联收敛两个入口：收敛只按“仍在占用”这一个条件扫描。
        Index(
            "agent_execution_conversation_occupied_uq",
            "conversation_id",
            unique=True,
            postgresql_where=text("status IN ('running', 'waiting_input')"),
        ),
        Index(
            "agent_execution_conversation_input_message_uq",
            "conversation_id",
            "input_message_id",
            unique=True,
            postgresql_where=text("input_message_id IS NOT NULL"),
        ),
    )

    id = Column(UUID, nullable=False, server_default=text("uuid_generate_v4()"))
    app_id = Column(UUID, nullable=False)  # 所属应用标识；内置助手不要求存在 App 记录；必须与 conversation_turn.app_id 一致
    conversation_id = Column(
        UUID, ForeignKey("conversation.id", ondelete="RESTRICT"), nullable=False
    )  # 会话占用唯一约束所需；必须与 turn.conversation_id 一致，创建后不改归属
    invoke_from = Column(String(32), nullable=False)  # 入口来源，用于隔离运行查询
    turn_id = Column(
        UUID, ForeignKey("conversation_turn.id", ondelete="CASCADE"), nullable=False
    )  # 承载本轮问答与累计用量；归属三元组一致性由创建服务保证
    input_message_id = Column(String(255), nullable=True)  # 对齐客户端消息、历史和时间线；相同标识重复启动时复用 Execution
    status = Column(
        String(32), nullable=False, server_default=text("'running'")
    )  # AgentExecutionStatus，独立于消息展示状态；建行即已分配执行权
    error_message = Column(
        Text, nullable=False, server_default=text("''::text")
    )  # 可向用户展示的 Execution 失败原因；异常堆栈只写入日志
    execution_options = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))  # 模型覆盖、能力开关、Skill 选择与配置指纹；不复制问答内容或身份
    pending_inputs = Column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )  # 当前批次待填表单；接受答案或结束本轮后清空，取消后不保留可恢复副本
    input_history = Column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )  # 已确认的问题、字段与答案；用于历史展示及后续模型调用，提交去重由 Submission 保证
    # 当前轮消息和页面进度投影；历史问答、运行选项、表单与图 checkpoint 不在这里复制。
    display_state = Column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    last_event_sequence = Column(Integer, nullable=False, server_default=text("0"))  # 已持久化事件游标，供流式重连续传

    executor_id = Column(UUID, nullable=True)  # 本次执行段的标识，不是账号或进程 ID；启动/恢复换新，结算清空
    cancel_requested_at = Column(
        DateTime, nullable=True
    )  # 收到取消请求的时间；执行线程与状态查询都读它，终态保留原值供追溯
    finished_at = Column(DateTime, nullable=True)  # 进入终态的时间，等待用户输入时不设置
    created_at = Column(
        DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP(0)")
    )  # 建行即开始任务；暂停恢复不改，公开 started_at 由此映射

    @property
    def is_terminal(self) -> bool:
        """统一记录的终态判断，等待用户输入仍可继续执行。"""
        return self.status in AGENT_EXECUTION_TERMINAL_STATUSES

    @property
    def public_snapshot(self) -> dict[str, Any]:
        """仅公开展示白名单，终态不返回可提交表单或旧取消恢复信息。"""
        return {
            **{key: value for key, value in (self.display_state or {}).items() if key in _DISPLAY_STATE_KEYS},
            "pending_inputs": list(self.pending_inputs or []) if self.status == "waiting_input" else [],
            "input_history": list(self.input_history or []),
        }

    def apply_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        """只更新页面展示字段；表单及已确认答案由业务命令单独维护。"""
        display = {
            **{key: value for key, value in (self.display_state or {}).items() if key in _DISPLAY_STATE_KEYS},
            **{key: snapshot[key] for key in _DISPLAY_STATE_KEYS if key in snapshot},
        }
        if "messages" in display:
            # 历史问答由 ConversationTurn 提供，快照只保留本轮刷新时需要的内容。
            current_ids = {f"{self.turn_id}:human", f"{self.turn_id}:assistant"}
            if self.input_message_id:
                current_ids.add(self.input_message_id)
            display["messages"] = [
                message for message in display["messages"]
                if isinstance(message, Mapping) and message.get("id") in current_ids
            ] if isinstance(display["messages"], list) else []
        self.display_state = display


class AgentExecutionSubmission(db.Model):
    """每批表单仅接受一份答案；主键由中断 ID 集合决定，与答案内容无关。"""

    __tablename__ = "agent_execution_submission"
    __table_args__ = (
        PrimaryKeyConstraint("execution_id", "input_batch_id", name="pk_agent_execution_submission_batch"),
    )

    execution_id = Column(UUID, ForeignKey("agent_execution.id", ondelete="CASCADE"), nullable=False)
    input_batch_id = Column(String(64), nullable=False)  # 整批中断 ID 集合的摘要；不是答案哈希或用户 ID
    answers = Column(JSONB, nullable=False)
    accepted_sequence = Column(Integer, nullable=False)
    created_at = Column(DateTime, nullable=False, server_default=text("(CURRENT_TIMESTAMP AT TIME ZONE 'UTC')"))


class AgentExecutionEvent(db.Model):
    """持久化协议事件，供流式重连续传和执行过程历史查询。"""

    __tablename__ = "agent_execution_event"
    __table_args__ = (
        PrimaryKeyConstraint(
            "execution_id",
            "sequence",
            name="pk_agent_execution_event_run_sequence",
        ),
    )

    execution_id = Column(UUID, ForeignKey("agent_execution.id", ondelete="CASCADE"), nullable=False)
    sequence = Column(Integer, nullable=False)  # 事件顺序与续传游标，同一 Run 内唯一
    payload = Column(JSONB, nullable=False)  # 必须显式提供经校验的 AgentProtocolEvent，空对象不是有效事件
    created_at = Column(
        DateTime, nullable=False, server_default=text("(CURRENT_TIMESTAMP AT TIME ZONE 'UTC')")
    )


__all__ = ["AgentExecution", "AgentExecutionSubmission", "AgentExecutionEvent"]
