"""Structured memory persistence, permissions, and recall."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from internal.exception import FailException
from internal.model import MemoryFact
from pkg.sqlalchemy import SQLAlchemy
from .base_service import BaseService


def _uuid(value, field: str) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise FailException(f"{field} 不是合法 UUID") from exc


def _optional_uuid(value, field: str) -> UUID | None:
    return None if value is None else _uuid(value, field)


class MemoryFactService(BaseService):
    """Final memory contract.

    End users own mutations. Creators receive a separate read-only listing.
    Runtime extraction uses the explicit internal ``create_auto_fact`` method.
    """

    db: SQLAlchemy

    def list_end_user_facts(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        end_user_id: str | UUID | None,
        keyword: str | None = None,
        sort: str = "updated_at",
        limit: int = 50,
        offset: int = 0,
    ) -> list[MemoryFact]:
        query = self.db.session.query(MemoryFact).filter(
            MemoryFact.account_id == _uuid(account_id, "account_id"),
            MemoryFact.app_id == _uuid(app_id, "app_id"),
            MemoryFact.end_user_id == _uuid(end_user_id, "end_user_id"),
            MemoryFact.deleted_at.is_(None),
        )
        if keyword:
            query = query.filter(MemoryFact.content.ilike(f"%{keyword.strip()}%"))
        order = (
            MemoryFact.importance.desc()
            if sort == "importance"
            else MemoryFact.updated_at.desc()
        )
        return query.order_by(order).offset(max(offset, 0)).limit(min(limit, 200)).all()

    def list_creator_facts(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        end_user_id_filter: str | UUID | None = None,
        keyword: str | None = None,
        limit: int = 100,
    ) -> list[MemoryFact]:
        query = self.db.session.query(MemoryFact).filter(
            MemoryFact.account_id == _uuid(account_id, "account_id"),
            MemoryFact.app_id == _uuid(app_id, "app_id"),
            MemoryFact.deleted_at.is_(None),
        )
        if end_user_id_filter:
            query = query.filter(
                MemoryFact.end_user_id
                == _uuid(end_user_id_filter, "end_user_id")
            )
        if keyword:
            query = query.filter(MemoryFact.content.ilike(f"%{keyword.strip()}%"))
        return query.order_by(MemoryFact.updated_at.desc()).limit(min(limit, 500)).all()

    def create_end_user_fact(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        end_user_id: str | UUID,
        content: str,
        importance: float = 0.7,
    ) -> MemoryFact:
        return self._create_fact(
            account_id=account_id,
            app_id=app_id,
            end_user_id=end_user_id,
            content=content,
            importance=importance,
            source_type="manual",
            auto_extracted=False,
        )

    def create_auto_fact(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        end_user_id: str | UUID,
        content: str,
        importance: float,
        source_conversation_id: str | UUID | None = None,
        source_message_id: str | UUID | None = None,
        fact_type: str = "general",
    ) -> MemoryFact | None:
        if importance < 0.6:
            return None
        normalized = content.strip()
        query = self.db.session.query(MemoryFact).filter(
            MemoryFact.account_id == _uuid(account_id, "account_id"),
            MemoryFact.app_id == _uuid(app_id, "app_id"),
            MemoryFact.content == normalized,
            MemoryFact.deleted_at.is_(None),
        )
        if end_user_id is None:
            query = query.filter(MemoryFact.end_user_id.is_(None))
        else:
            query = query.filter(
                MemoryFact.end_user_id == _uuid(end_user_id, "end_user_id")
            )
        duplicate = query.one_or_none()
        if duplicate is not None:
            if importance > duplicate.importance:
                duplicate.importance = importance
                duplicate.updated_at = datetime.now()
                self.db.session.commit()
            return duplicate
        return self._create_fact(
            account_id=account_id,
            app_id=app_id,
            end_user_id=end_user_id,
            content=normalized,
            importance=importance,
            source_type="auto",
            auto_extracted=True,
            source_conversation_id=source_conversation_id,
            source_message_id=source_message_id,
            fact_type=fact_type,
        )

    def update_end_user_fact(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        end_user_id: str | UUID,
        fact_id: str | UUID,
        content: str,
        importance: float | None = None,
    ) -> MemoryFact:
        fact = self._owned_end_user_fact(
            account_id=account_id,
            app_id=app_id,
            end_user_id=end_user_id,
            fact_id=fact_id,
        )
        if not content.strip():
            raise FailException("记忆内容不能为空")
        if importance is not None and not 0 <= importance <= 1:
            raise FailException("importance 必须在 [0, 1]")
        fact.content = content.strip()
        if importance is not None:
            fact.importance = importance
        fact.source_type = "edited"
        fact.auto_extracted = False
        fact.updated_at = datetime.now()
        self.db.session.commit()
        return fact

    def soft_delete_end_user_fact(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        end_user_id: str | UUID,
        fact_id: str | UUID,
    ) -> MemoryFact:
        fact = self._owned_end_user_fact(
            account_id=account_id,
            app_id=app_id,
            end_user_id=end_user_id,
            fact_id=fact_id,
        )
        fact.deleted_at = datetime.now()
        self.db.session.commit()
        return fact

    def batch_delete_end_user_facts(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        end_user_id: str | UUID,
        fact_ids: list[str] | None = None,
        before: datetime | None = None,
        after: datetime | None = None,
    ) -> int:
        query = self.db.session.query(MemoryFact).filter(
            MemoryFact.account_id == _uuid(account_id, "account_id"),
            MemoryFact.app_id == _uuid(app_id, "app_id"),
            MemoryFact.end_user_id == _uuid(end_user_id, "end_user_id"),
            MemoryFact.deleted_at.is_(None),
        )
        if fact_ids:
            query = query.filter(
                MemoryFact.id.in_([_uuid(item, "fact_id") for item in fact_ids])
            )
        if before:
            query = query.filter(MemoryFact.created_at <= before)
        if after:
            query = query.filter(MemoryFact.created_at >= after)
        rows = query.all()
        deleted_at = datetime.now()
        for row in rows:
            row.deleted_at = deleted_at
        self.db.session.commit()
        return len(rows)

    def recall_top_k(
        self,
        *,
        account_id: str | UUID,
        app_id: str | UUID,
        end_user_id: str | UUID | None,
        keyword: str | None = None,
        k: int = 5,
    ) -> list[MemoryFact]:
        query = self.db.session.query(MemoryFact).filter(
            MemoryFact.account_id == _uuid(account_id, "account_id"),
            MemoryFact.app_id == _uuid(app_id, "app_id"),
            MemoryFact.deleted_at.is_(None),
        )
        if end_user_id is None:
            query = query.filter(MemoryFact.end_user_id.is_(None))
        else:
            query = query.filter(
                MemoryFact.end_user_id == _uuid(end_user_id, "end_user_id")
            )
        if keyword:
            tokens = [
                token.strip()
                for token in keyword.replace("\n", " ").split(" ")
                if token.strip()
            ][:5]
            if tokens:
                query = query.filter(
                    *[MemoryFact.content.ilike(f"%{token}%") for token in tokens]
                )
        return (
            query.order_by(MemoryFact.importance.desc(), MemoryFact.updated_at.desc())
            .limit(max(1, min(k, 20)))
            .all()
        )

    def _create_fact(
        self,
        *,
        account_id,
        app_id,
        end_user_id,
        content: str,
        importance: float,
        source_type: str,
        auto_extracted: bool,
        source_conversation_id=None,
        source_message_id=None,
        fact_type: str = "general",
    ) -> MemoryFact:
        if not content.strip():
            raise FailException("记忆内容不能为空")
        if not 0 <= importance <= 1:
            raise FailException("importance 必须在 [0, 1]")
        fact = MemoryFact(
            id=uuid4(),
            account_id=_uuid(account_id, "account_id"),
            app_id=_uuid(app_id, "app_id"),
            end_user_id=_optional_uuid(end_user_id, "end_user_id"),
            fact_type=fact_type,
            content=content.strip(),
            importance=importance,
            source_conversation_id=(
                _uuid(source_conversation_id, "source_conversation_id")
                if source_conversation_id
                else None
            ),
            source_message_id=(
                _uuid(source_message_id, "source_message_id")
                if source_message_id
                else None
            ),
            source_type=source_type,
            auto_extracted=auto_extracted,
        )
        self.db.session.add(fact)
        self.db.session.commit()
        return fact

    def _owned_end_user_fact(
        self,
        *,
        account_id,
        app_id,
        end_user_id,
        fact_id,
    ) -> MemoryFact:
        fact = (
            self.db.session.query(MemoryFact)
            .filter(
                MemoryFact.id == _uuid(fact_id, "fact_id"),
                MemoryFact.account_id == _uuid(account_id, "account_id"),
                MemoryFact.app_id == _uuid(app_id, "app_id"),
                MemoryFact.end_user_id == _uuid(end_user_id, "end_user_id"),
                MemoryFact.deleted_at.is_(None),
            )
            .one_or_none()
        )
        if fact is None:
            raise FailException("记忆不存在或无权访问")
        return fact


__all__ = ["MemoryFactService"]
