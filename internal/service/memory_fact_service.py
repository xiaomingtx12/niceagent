"""MemoryFact 服务（R4 用户结构化事实 CRUD + 端用户可见性）。

详见 plan `docs/deepagents-v2-plan/00-harness-overall-upgrade.md` §1.5 / §3.1。

约束（硬约束 #5）：
- 复用 master `BaseService` + `SQLAlchemy` + `injector`，不自建 ORM 也不自建 DI
- 可见性校验在 service 层做（路径白名单思路同 workspace_file_service）
- LLM 自动抽取由 R5+ 单独接入（ProjectContextMiddleware.wrap_model_call
  hook 解析 LLM 输出 → create_fact），本服务只负责 CRUD + 可见性

可见性矩阵（plan §3.1）：
- account 级 fact（end_user_id IS NULL）：creator 全权管理
- end-user 级 fact（end_user_id IS NOT NULL）：
  - end-user 可见本人（WebApp /web-apps/{token}/conversations/{cnv_id}/memory）
  - creator 可见 app 下全部 fact（creator /apps/{app_id}/memory 面板）
  - 跨 account 强隔离（403）

软删除：`deleted_at` 非 NULL 表示已删除，业务查询一律走
`WHERE deleted_at IS NULL` 默认过滤；保留 audit 用。
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional
from uuid import UUID, uuid4

from internal.exception import FailException
from internal.model.memory_fact import MemoryFact
from pkg.sqlalchemy import SQLAlchemy
from .base_service import BaseService


def _to_uuid(value, *, field: str):
    """业务接口收 str（HTTP/UUID 边界），DB 层期望 UUID.

    业务 API 层传 str 进来, 这里统一转; None → None; 非法 → FailException.
    """
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as e:
        raise FailException(f"{field} 不是合法 UUID: {value!r}") from e


class MemoryFactService(BaseService):
    """memory_facts 表 CRUD + 可见性校验。

    方法签名约定（前端 API 端点直接调）：
    - list_end_user_facts：end-user 看自己（强制 end_user_id 校验）
    - list_creator_facts：creator 看全 app（含 end-user fact）
    - create_fact：手动创建（end-user 端点带 end_user_id；creator 可建 account 级）
    - soft_delete_fact：软删除（end-user 只能删自己；creator 可删任何）
    - recall_top_k：R4 ProjectContextMiddleware 注入用（无可见性校验，
      由调用方按 account_id + app_id + end_user_id 过滤）

    约束（硬约束 #5）：复用 master BaseService / SQLAlchemy + 直接走 session
    （避开 BaseService.create 内部 db.auto_commit 依赖，生产环境 injector
    注入 SQLAlchemy 实例时仍然兼容）。
    """

    db: SQLAlchemy

    # ---------- list ----------

    def list_end_user_facts(
        self,
        *,
        account_id: str,
        app_id: str,
        end_user_id: str,
        limit: int = 50,
    ) -> List[MemoryFact]:
        """end-user "我的记忆" 列表：本人 fact + account 级 fact（共享）。"""
        acc_uuid = _to_uuid(account_id, field="account_id")
        app_uuid = _to_uuid(app_id, field="app_id")
        eu_uuid = _to_uuid(end_user_id, field="end_user_id")
        return (
            self.db.session.query(MemoryFact)
            .filter(
                MemoryFact.account_id == acc_uuid,
                MemoryFact.app_id == app_uuid,
                MemoryFact.deleted_at.is_(None),
                # 本人 OR account 级
                (
                    (MemoryFact.end_user_id == eu_uuid)
                    | MemoryFact.end_user_id.is_(None)
                ),
            )
            .order_by(MemoryFact.updated_at.desc())
            .limit(limit)
            .all()
        )

    def list_creator_facts(
        self,
        *,
        account_id: str,
        app_id: str,
        end_user_id_filter: Optional[str] = None,
        fact_type_filter: Optional[str] = None,
        limit: int = 100,
    ) -> List[MemoryFact]:
        """creator 面板：全 app fact，可按 end_user_id / fact_type 过滤."""
        acc_uuid = _to_uuid(account_id, field="account_id")
        app_uuid = _to_uuid(app_id, field="app_id")
        q = self.db.session.query(MemoryFact).filter(
            MemoryFact.account_id == acc_uuid,
            MemoryFact.app_id == app_uuid,
            MemoryFact.deleted_at.is_(None),
        )
        if end_user_id_filter is not None:
            if end_user_id_filter == "":
                # creator 面板 "account 级" 过滤
                q = q.filter(MemoryFact.end_user_id.is_(None))
            else:
                q = q.filter(MemoryFact.end_user_id == _to_uuid(end_user_id_filter, field="end_user_id"))
        if fact_type_filter:
            q = q.filter(MemoryFact.fact_type == fact_type_filter)
        return q.order_by(MemoryFact.updated_at.desc()).limit(limit).all()

    # ---------- create ----------

    def create_fact(
        self,
        *,
        account_id: str,
        app_id: str,
        end_user_id: Optional[str],
        fact_type: str,
        content: str,
        confidence: float = 1.0,
        source_conversation_id: Optional[str] = None,
        auto_extracted: bool = False,
    ) -> MemoryFact:
        """手动写入一条 fact（end-user 端点 end_user_id 必填；creator 可留空）。"""
        if not content or not content.strip():
            raise FailException("fact content 不能为空")
        if not (0.0 <= confidence <= 1.0):
            raise FailException("confidence 必须在 [0.0, 1.0]")
        if auto_extracted and end_user_id is None:
            # LLM 抽取必须有 end_user_id（不能给 creator 抽取自己）
            # 业务约定: 自动抽取总归属某个端用户
            raise FailException("auto_extracted fact 必须有 end_user_id")
        fact = MemoryFact(
            id=uuid4(),
            account_id=_to_uuid(account_id, field="account_id"),
            app_id=_to_uuid(app_id, field="app_id"),
            end_user_id=_to_uuid(end_user_id, field="end_user_id"),
            fact_type=fact_type,
            content=content.strip(),
            confidence=confidence,
            source_conversation_id=_to_uuid(source_conversation_id, field="source_conversation_id"),
            auto_extracted=auto_extracted,
        )
        self.db.session.add(fact)
        self.db.session.commit()
        return fact

    # ---------- soft delete ----------

    def soft_delete_fact(
        self,
        *,
        account_id: str,
        fact_id: UUID,
        actor_end_user_id: Optional[str] = None,
        actor_is_creator: bool = False,
    ) -> MemoryFact:
        """软删除 fact（保留 audit）。

        可见性校验：
        - end-user 只能删 end_user_id == actor_end_user_id 的本人 fact
        - creator 可删本 account_id 下任何 fact
        - 跨 account 永远 403
        """
        acc_uuid = _to_uuid(account_id, field="account_id")
        eu_uuid = _to_uuid(actor_end_user_id, field="actor_end_user_id") if actor_end_user_id else None
        fact_id_uuid = _to_uuid(fact_id, field="fact_id")
        fact = self.db.session.query(MemoryFact).filter(
            MemoryFact.id == fact_id_uuid,
            MemoryFact.account_id == acc_uuid,
        ).first()
        if fact is None:
            raise FailException("fact 不存在或无权访问")
        if fact.deleted_at is not None:
            raise FailException("fact 已删除")
        if not actor_is_creator:
            # end-user 只能删本人的
            if fact.end_user_id != eu_uuid:
                raise FailException("无权删除他人 fact")
            # end-user 不能删 account 级 fact
            if fact.end_user_id is None:
                raise FailException("无权删除 account 级 fact")
        fact.deleted_at = datetime.now()
        self.db.session.commit()
        return fact

    # ---------- recall (R4 ProjectContextMiddleware 注入用) ----------

    def recall_top_k(
        self,
        *,
        account_id: str,
        app_id: str,
        end_user_id: Optional[str],
        k: int = 5,
    ) -> List[MemoryFact]:
        """按 (confidence, updated_at) 排序召回 top-K fact.

        不做语义向量召回（R5+ 接入 embedding），只用 LLM 抽取时给的 confidence
        + 时间衰减。end_user_id 提供时优先召回本人 + account 级；为 None 时
        只召回 account 级（creator 调试场景）。

        Returns:
            top-K MemoryFact 列表（按 score 降序）
        """
        acc_uuid = _to_uuid(account_id, field="account_id")
        app_uuid = _to_uuid(app_id, field="app_id")
        eu_uuid = _to_uuid(end_user_id, field="end_user_id") if end_user_id else None
        q = self.db.session.query(MemoryFact).filter(
            MemoryFact.account_id == acc_uuid,
            MemoryFact.app_id == app_uuid,
            MemoryFact.deleted_at.is_(None),
        )
        if eu_uuid is not None:
            q = q.filter(
                (MemoryFact.end_user_id == eu_uuid)
                | MemoryFact.end_user_id.is_(None)
            )
        else:
            q = q.filter(MemoryFact.end_user_id.is_(None))
        # confidence desc + updated_at desc 排序; 简单召回不做事后分数计算
        return (
            q.order_by(
                MemoryFact.confidence.desc(),
                MemoryFact.updated_at.desc(),
            )
            .limit(k)
            .all()
        )


__all__ = ["MemoryFactService"]