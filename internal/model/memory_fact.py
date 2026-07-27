"""MemoryFact 模型 (R3 用户结构化事实持久化)

详见 migration d_v2_003_create_memory_facts.py 顶部 docstring + plan
`docs/deepagents-v2-plan/00-harness-overall-upgrade.md` §1.5 / §3.1。

约束 (硬约束 #5):
- 复用 SQLAlchemy + master 现有 db.Model 模式, 不自建 ORM
- fact 召回 / 注入由 L3 ProjectContextMiddleware 业务侧负责 (R4+);
  本模型只负责 DB 持久化 + 查询谓词

可见性:
- end_user_id IS NULL → creator / account 级 fact
- end_user_id IS NOT NULL → 端用户 fact, end-user 可见本人 + creator 可见全 app
"""
from datetime import datetime

from sqlalchemy import (
    PrimaryKeyConstraint,
    CheckConstraint,
    Index,
    UUID,
    Column,
    String,
    Float,
    Text,
    DateTime,
    Boolean,
    text,
)

from internal.extension.database_extension import db


class MemoryFact(db.Model):
    """用户结构化事实 (R3)。

    三类记忆分离 (plan §2.2):
    - 压缩 (SummarizationMiddleware, 不落库, 自动随 session 推进截断)
    - 摘要 (R4+ memory_summaries 表, 跨 session 对话级压缩)
    - fact (本表, 跨 session 结构化事实, LLM 抽取或用户/creator 手动写入)

    业务约束:
    - content: 1-2 句短文本 (LLM 抽取器保证长度)
    - confidence: 0.0-1.0, 供召回排序 (top-K 取 confidence * 衰减因子)
    - auto_extracted=True: LLM 抽取, end-user 可见可删除 ("我的记忆" 页)
    - auto_extracted=False: 用户/creator 手动写入, end-user 可见但 creator 不允许
      批量删除 (保护用户显式偏好)
    """

    __tablename__ = "memory_facts"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_memory_facts"),
        # end-user "我的记忆" 列表 (account_id, app_id, end_user_id)
        Index("idx_memory_facts_end_user", "account_id", "app_id", "end_user_id"),
        # creator 面板 (account_id, app_id) + 时间排序
        Index("idx_memory_facts_app_updated", "account_id", "app_id", "updated_at"),
        # 召回排序: confidence + updated_at (R4+ ProjectContextMiddleware 用)
        Index(
            "idx_memory_facts_recall",
            "account_id", "app_id", "confidence", "updated_at",
        ),
        # deleted_at 过滤 (默认 WHERE deleted_at IS NULL)
        Index("idx_memory_facts_deleted_at", "deleted_at"),
        # confidence 范围
        CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0",
            name="ck_memory_facts_confidence_range",
        ),
    )

    id = Column(UUID, server_default=text("uuid_generate_v4()"), nullable=False)
    account_id = Column(UUID, nullable=False)
    app_id = Column(UUID, nullable=False)
    # NULL 表示 account 级 / creator fact
    end_user_id = Column(UUID, nullable=True)
    # 'preference' / 'profile' / 'instruction' / 等; 业务按需
    fact_type = Column(String(64), nullable=False)
    content = Column(Text, nullable=False)
    # 召回排序用
    confidence = Column(
        Float, nullable=False, server_default=text("1.0")
    )
    # audit / 回溯用
    source_conversation_id = Column(UUID, nullable=True)
    # True=LLM 自动抽取; False=用户/creator 手动写入
    auto_extracted = Column(
        Boolean, nullable=False, server_default=text("true")
    )
    # 软删除; NULL=未删除
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(
        DateTime, default=datetime.now, nullable=False
    )
    updated_at = Column(
        DateTime,
        default=datetime.now,
        onupdate=datetime.now,
        nullable=False,
    )

    def to_dict(self) -> dict:
        """业务序列化 (供 API 返回, R4+).

        R3 只建表 + 模型, API 由 R4+ 服务层添加. 这里给一个基础序列化,
        业务 API 后续可以扩展字段 (e.g. recall_score).
        """
        return {
            "id": str(self.id),
            "account_id": str(self.account_id),
            "app_id": str(self.app_id),
            "end_user_id": str(self.end_user_id) if self.end_user_id else None,
            "fact_type": self.fact_type,
            "content": self.content,
            "confidence": self.confidence,
            "auto_extracted": self.auto_extracted,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }