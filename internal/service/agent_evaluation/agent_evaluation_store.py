"""测评任务状态持久化：原子认领、截止时间和终态保护集中在这里。"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from injector import inject
from sqlalchemy.orm import Query

from internal.exception import FailException
from internal.model.agent_evaluation import AgentEvaluation
from pkg.sqlalchemy import SQLAlchemy

EVALUATION_DEADLINE_SECONDS = 900
PENDING_STATUSES = ("queued", "running")


@inject
@dataclass
class EvaluationTaskStore:
    """只管理已授权任务的执行状态；来源权限与幂等提交由 Service 负责。"""

    db: SQLAlchemy

    def claim(self, evaluation_id: UUID) -> AgentEvaluation | None:
        """条件更新争取唯一执行权，重复投递和过期任务都不能再次启动模型。"""
        self.expire_pending(self._query(evaluation_id))
        with self.db.auto_commit():
            claimed = self._active_query(evaluation_id, "queued").update(
                {"status": "running"},
                synchronize_session=False,
            )
        if not claimed:
            return None
        evaluation = self.db.session.get(AgentEvaluation, evaluation_id)
        self.db.session.refresh(evaluation)
        return evaluation

    def save_progress(self, evaluation_id: UUID, progress: dict) -> None:
        """每次进度写入检查截止时间，不依赖页面轮询才能终止过期测评。"""
        with self.db.auto_commit():
            changed = self._active_query(evaluation_id, "running").update(
                {"result": {"runtime": progress}},
                synchronize_session=False,
            )
        if not changed:
            self.expire_pending(self._query(evaluation_id))
            raise FailException("测评记录已结束或超时，停止后续评分")

    def complete(self, evaluation_id: UUID, result: dict) -> None:
        """完成表示得到报告，不代表验收通过；迟到报告不能覆盖超时或错误状态。"""
        with self.db.auto_commit():
            changed = self._active_query(evaluation_id, "running").update(
                {
                    "status": "completed",
                    "result": result,
                    "error_message": "",
                    "finished_at": self._now(),
                },
                synchronize_session=False,
            )
        if not changed:
            self.expire_pending(self._query(evaluation_id))

    def fail(
        self,
        evaluation_id: UUID,
        *,
        expected_status: Literal["queued", "running"],
        error: str,
    ) -> None:
        """投递失败和执行失败各自校验原状态，并保留此前已经持久化的评分轮次。"""
        self.expire_pending(self._query(evaluation_id))
        with self.db.auto_commit():
            self._query(evaluation_id).filter(
                AgentEvaluation.status == expected_status,
            ).update(
                {"status": "error", "error_message": error, "finished_at": self._now()},
                synchronize_session=False,
            )

    def expire_pending(self, query: Query) -> None:
        """只收敛调用者指定的记录范围，不在详情轮询或 Worker 中扫描全表。"""
        now = self._now()
        with self.db.auto_commit():
            query.filter(
                AgentEvaluation.status.in_(PENDING_STATUSES),
                AgentEvaluation.created_at
                < now - timedelta(seconds=EVALUATION_DEADLINE_SECONDS),
            ).update(
                {
                    "status": "error",
                    "error_message": "测评等待或执行超过 15 分钟，请检查后台队列后新建测评。",
                    "finished_at": now,
                },
                synchronize_session="fetch",
            )

    def _active_query(self, evaluation_id: UUID, status: str) -> Query:
        """状态和截止时间在同一条 UPDATE 中判断，避免先检查再写入的竞态。"""
        return self._query(evaluation_id).filter(
            AgentEvaluation.status == status,
            AgentEvaluation.created_at
            >= self._now() - timedelta(seconds=EVALUATION_DEADLINE_SECONDS),
        )

    def _query(self, evaluation_id: UUID) -> Query:
        """Worker 只用服务端分配的测评 ID 定位记录。"""
        return self.db.session.query(AgentEvaluation).filter(
            AgentEvaluation.id == evaluation_id
        )

    @staticmethod
    def _now() -> datetime:
        """数据库沿用无时区 UTC，避免和提交时间使用不同的时区口径。"""
        return datetime.now(timezone.utc).replace(tzinfo=None)
