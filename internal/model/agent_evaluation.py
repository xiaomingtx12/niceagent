"""历史任务测评记录，与 Agent 执行状态分开存储。"""

from sqlalchemy import Column, DateTime, Index, String, Text, UUID, text
from sqlalchemy.dialects.postgresql import JSONB

from internal.extension.database_extension import db


class AgentEvaluation(db.Model):
    """保存评分时实际使用的材料和配置，后续修改应用不改写历史结论。"""

    __tablename__ = "agent_evaluation"
    __table_args__ = (
        Index("agent_evaluation_owner_app_created_idx", "account_id", "app_id", "created_at", "id"),
    )

    id = Column(UUID, primary_key=True, server_default=text("uuid_generate_v4()"))
    account_id = Column(UUID, nullable=False)  # 发起并拥有此次测评的 Account.id
    app_id = Column(UUID, nullable=False)  # 被测应用的 App.id
    run_id = Column(UUID, nullable=False)  # 来源 AgentRun.id；测评自身身份是本表 id
    source_snapshot = Column(JSONB, nullable=False)
    criteria = Column(JSONB, nullable=False)
    judge_config = Column(JSONB, nullable=False)
    evaluator_version = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False)
    result = Column(JSONB, nullable=True)
    error_message = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, server_default=text("CURRENT_TIMESTAMP(0)"))
    finished_at = Column(DateTime, nullable=True)
