"""Creator 任务中心的 Agent 与会话历史服务。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from injector import inject
from sqlalchemy import and_, desc, func, or_, select

from internal.entity.app_entity import AppStatus
from internal.entity.conversation_entity import InvokeFrom
from internal.exception import NotFoundException
from internal.model import Account, AgentRun, App, Conversation, EndUser
from internal.schema.agent_runtime_schema import (
    AgentConversationCleanupReq,
    AgentInvocationContext,
)
from pkg.sqlalchemy import SQLAlchemy

from .agent_conversation_cleanup_service import AgentConversationCleanupService
from .app_config_service import AppConfigService
from .base_service import BaseService
from .conversation_service import ConversationService


@inject
@dataclass
class CreatorTaskService(BaseService):
    """查询 Creator 可见的已发布 Agent 和跨入口会话历史。"""

    db: SQLAlchemy
    app_config_service: AppConfigService
    agent_cleanup_service: AgentConversationCleanupService
    conversation_service: ConversationService

    def create_conversation(self, app_id: UUID, account: Account) -> dict[str, Any]:
        """创建任务中心会话并由数据库分配会话 ID。"""
        app = (
            self.db.session.query(App)
            .filter(
                App.id == app_id,
                App.account_id == account.id,
                App.status == AppStatus.PUBLISHED,
            )
            .one_or_none()
        )
        if app is None:
            raise NotFoundException("应用不存在、未发布或无权访问")
        conversation = self.create(
            Conversation,
            app_id=app.id,
            name="新的会话",
            invoke_from=InvokeFrom.CREATOR_TASK,
            created_by=account.id,
            owner_account_id=account.id,
        )
        return self._serialize_conversation(conversation, app)

    def get_invocation_context(
        self,
        *,
        app_id: UUID,
        conversation_id: UUID,
        account: Account,
    ) -> AgentInvocationContext:
        """把 Creator 任务入口转换为已校验的 Agent 调用上下文。"""
        app = (
            self.db.session.query(App)
            .filter(
                App.id == app_id,
                App.account_id == account.id,
                App.status == AppStatus.PUBLISHED,
            )
            .one_or_none()
        )
        if app is None:
            raise NotFoundException("应用不存在、未发布或无权访问")
        conversation = self.conversation_service.get_owned_conversation(
            conversation_id=conversation_id,
            account=account,
            app_id=app.id,
            allowed_sources=(InvokeFrom.CREATOR_TASK, InvokeFrom.WEB_APP),
        )
        return AgentInvocationContext(
            account_id=account.id,
            app_id=app.id,
            conversation_id=conversation.id,
            created_by=account.id,
            end_user_id=None,
            invoke_from=InvokeFrom.CREATOR_TASK,
        )

    def _visible_query(self, account: Account):
        """构建同账号可见的任务会话查询。

        ``owner_account_id`` 是新数据的稳定归属；created_by 和 EndUser
        条件用于兼容历史 WebApp 会话，待业务侧完成数据回填后仍可保留。
        """
        legacy_end_users = select(EndUser.id).where(
            EndUser.external_user_id == account.id
        )
        ownership = or_(
            Conversation.owner_account_id == account.id,
            and_(
                Conversation.owner_account_id.is_(None),
                or_(
                    Conversation.created_by == account.id,
                    and_(
                        Conversation.invoke_from == InvokeFrom.WEB_APP,
                        Conversation.created_by.in_(legacy_end_users),
                    ),
                ),
            ),
        )
        return (
            self.db.session.query(Conversation, App)
            .join(App, App.id == Conversation.app_id)
            .filter(
                App.account_id == account.id,
                App.status == AppStatus.PUBLISHED,
                Conversation.invoke_from.in_(
                    [
                        InvokeFrom.CREATOR_TASK,
                        InvokeFrom.WEB_APP,
                    ]
                ),
                Conversation.is_deleted.is_(False),
                ownership,
            )
        )

    @staticmethod
    def _timestamp(value: datetime | None) -> int:
        return int(value.timestamp()) if value else 0

    def _serialize_conversation(
        self, conversation: Conversation, app: App, *, last_run_status: str = "idle",
    ) -> dict[str, Any]:
        """序列化已加载的摘要；列表中的运行状态由调用方批量读取。"""
        return {
            "id": str(conversation.id),
            "name": conversation.name or "新的会话",
            "summary": conversation.summary or "",
            "is_pinned": bool(conversation.is_pinned),
            "updated_at": self._timestamp(conversation.updated_at),
            "created_at": self._timestamp(conversation.created_at),
            "last_run_status": last_run_status,
            "agent": {
                "id": str(app.id),
                "name": app.name,
                "icon": app.icon,
                "description": app.description,
            },
        }

    def get_agents(self, account: Account) -> list[dict[str, Any]]:
        """获取当前账号已发布 Agent 及其任务会话概览。"""
        apps = (
            self.db.session.query(App)
            .filter(App.account_id == account.id, App.status == AppStatus.PUBLISHED)
            .order_by(desc(App.updated_at), desc(App.id))
            .all()
        )
        if not apps:
            return []
        # 复用会话可见性条件，统计与最近活动时间在同一次分组查询中取得。
        summaries = {
            row.app_id: row
            for row in self._visible_query(account)
            .with_entities(
                Conversation.app_id,
                func.count(Conversation.id).label("conversation_count"),
                func.max(Conversation.updated_at).label("last_active_at"),
            )
            .filter(Conversation.app_id.in_([app.id for app in apps]))
            .group_by(Conversation.app_id)
            .all()
        }
        return [
            {
                "id": str(app.id),
                "name": app.name,
                "icon": app.icon,
                "description": app.description,
                "status": app.status,
                "conversation_count": summaries[app.id].conversation_count if app.id in summaries else 0,
                "last_active_at": (
                    self._timestamp(summaries[app.id].last_active_at) if app.id in summaries else 0
                ),
            }
            for app in apps
        ]

    def get_agent(self, app_id: UUID, account: Account) -> dict[str, Any]:
        """获取任务中心运行所需的已发布 Agent 配置，不返回 WebApp token。"""
        app = (
            self.db.session.query(App)
            .filter(
                App.id == app_id,
                App.account_id == account.id,
                App.status == AppStatus.PUBLISHED,
            )
            .one_or_none()
        )
        if app is None:
            raise NotFoundException("应用不存在、未发布或无权访问")
        return {
            "id": str(app.id),
            "name": app.name,
            "icon": app.icon,
            "description": app.description,
            "app_config": self.app_config_service.get_agent_runtime_config(
                app
            ).model_dump(mode="json"),
        }

    def get_conversations(
        self,
        account: Account,
        *,
        app_id: UUID | None = None,
        before: UUID | None = None,
        page_size: int = 20,
        search: str = "",
    ) -> dict[str, Any]:
        query = self._visible_query(account)
        if app_id is not None:
            query = query.filter(App.id == app_id)
        if search.strip():
            keyword = f"%{search.strip()}%"
            query = query.filter(
                or_(
                    Conversation.name.ilike(keyword),
                    Conversation.summary.ilike(keyword),
                )
            )
        if before:
            cursor = self.db.session.get(Conversation, before)
            if cursor is not None:
                query = query.filter(
                    or_(
                        Conversation.updated_at < cursor.updated_at,
                        and_(
                            Conversation.updated_at == cursor.updated_at,
                            Conversation.id < cursor.id,
                        ),
                    )
                )
        rows = (
            query.order_by(desc(Conversation.updated_at), desc(Conversation.id))
            .limit(page_size + 1)
            .all()
        )
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        statuses = self._last_run_statuses([conversation.id for conversation, _ in rows])
        return {
            "list": [
                self._serialize_conversation(
                    conversation, app, last_run_status=statuses.get(conversation.id, "idle"),
                )
                for conversation, app in rows
            ],
            "page": {
                "has_more": has_more,
                "next_before": str(rows[-1][0].id) if has_more and rows else None,
            },
        }

    def get_conversation(
        self, conversation_id: UUID, account: Account
    ) -> dict[str, Any]:
        row = (
            self._visible_query(account)
            .filter(Conversation.id == conversation_id)
            .one_or_none()
        )
        if row is None:
            raise NotFoundException("会话不存在或无权访问")
        statuses = self._last_run_statuses([row[0].id])
        return self._serialize_conversation(
            row[0], row[1], last_run_status=statuses.get(row[0].id, "idle"),
        )

    def update_conversation(
        self, conversation_id: UUID, account: Account, **values: Any
    ) -> dict[str, Any]:
        row = (
            self._visible_query(account)
            .filter(Conversation.id == conversation_id)
            .one_or_none()
        )
        if row is None:
            raise NotFoundException("会话不存在或无权访问")
        conversation, app = row
        for field, value in values.items():
            setattr(conversation, field, value)
        self.db.session.commit()
        statuses = self._last_run_statuses([conversation.id])
        return self._serialize_conversation(
            conversation, app, last_run_status=statuses.get(conversation.id, "idle"),
        )

    def delete_conversation(self, conversation_id: UUID, account: Account) -> None:
        row = (
            self._visible_query(account)
            .filter(Conversation.id == conversation_id)
            .one_or_none()
        )
        if row is None:
            raise NotFoundException("会话不存在或无权访问")
        conversation, app = row
        self.agent_cleanup_service.cleanup_conversation(
            AgentConversationCleanupReq(
                account_id=account.id,
                app_id=app.id,
                conversation_id=conversation.id,
            )
        )
        conversation.is_deleted = True
        self.db.session.commit()

    def _last_run_statuses(self, conversation_ids: list[UUID]) -> dict[UUID, str]:
        """仅为当前页的授权会话批量取最新状态；同时间运行按 ID 稳定排序。"""
        if not conversation_ids:
            return {}
        rows = (
            self.db.session.query(AgentRun.conversation_id, AgentRun.status)
            .filter(AgentRun.conversation_id.in_(conversation_ids))
            .distinct(AgentRun.conversation_id)
            .order_by(AgentRun.conversation_id, AgentRun.created_at.desc(), AgentRun.id.desc())
            .all()
        )
        return {row.conversation_id: row.status for row in rows}


__all__ = ["CreatorTaskService"]
