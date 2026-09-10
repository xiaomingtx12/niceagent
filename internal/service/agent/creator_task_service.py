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
from internal.schema.agent.agent_runtime_schema import (
    AgentConversationCleanupReq,
    AgentInvocationContext,
)
from pkg.sqlalchemy import SQLAlchemy

from .agent_conversation_cleanup_service import AgentConversationCleanupService
from internal.service.app.app_config_service import AppConfigService
from internal.service.common.base_service import BaseService
from internal.service.conversation.conversation_service import ConversationService


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
        # 1.复用会话可见性条件，计数和最近活动时间通过一次分组查询取得。
        summary_rows = (
            self._visible_query(account)
            .with_entities(
                Conversation.app_id,
                func.count(Conversation.id).label("conversation_count"),
                func.max(Conversation.updated_at).label("last_active_at"),
            )
            .filter(Conversation.app_id.in_([app.id for app in apps]))
            .group_by(Conversation.app_id)
            .all()
        )
        summaries = {row.app_id: row for row in summary_rows}

        # 2.没有会话的应用也要显示，缺失统计按 0 返回，不在循环内补查数据库。
        agents = []
        for app in apps:
            summary = summaries.get(app.id)
            conversation_count = 0
            last_active_at = 0
            if summary is not None:
                conversation_count = summary.conversation_count
                last_active_at = self._timestamp(summary.last_active_at)
            agents.append({
                "id": str(app.id),
                "name": app.name,
                "icon": app.icon,
                "description": app.description,
                "status": app.status,
                "conversation_count": conversation_count,
                "last_active_at": last_active_at,
            })
        return agents

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
        """只查询授权会话，按更新时间和 ID 组合游标返回一页结果。"""
        # 1.先应用账号可见性、应用和搜索条件，再追加游标范围。
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
        # 2.多取一条判断下一页；当前页的运行状态通过一次批量查询取得。
        rows = (
            query.order_by(desc(Conversation.updated_at), desc(Conversation.id))
            .limit(page_size + 1)
            .all()
        )
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        statuses = self._last_run_statuses([conversation.id for conversation, _ in rows])
        # 3.先生成列表和下一页游标，再构造响应，避免将流程埋在嵌套推导式中。
        conversations = []
        for conversation, app in rows:
            last_status = statuses.get(conversation.id, "idle")
            conversations.append(self._serialize_conversation(
                conversation, app, last_run_status=last_status,
            ))
        next_before = None
        if has_more and rows:
            next_before = str(rows[-1][0].id)
        return {
            "list": conversations,
            "page": {"has_more": has_more, "next_before": next_before},
        }

    def get_conversation(
        self, conversation_id: UUID, account: Account
    ) -> dict[str, Any]:
        """从授权列表范围读取会话及所属应用，避免绕过历史归属兼容规则。"""
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
        """更新入口已校验的会话字段，提交后返回包含最新运行状态的摘要。"""
        # 1.通过与列表相同的可见性条件确认修改权限。
        row = (
            self._visible_query(account)
            .filter(Conversation.id == conversation_id)
            .one_or_none()
        )
        if row is None:
            raise NotFoundException("会话不存在或无权访问")
        # 2.字段提交成功后再读取运行状态，保持响应与已保存的数据一致。
        conversation, app = row
        for field, value in values.items():
            setattr(conversation, field, value)
        self.db.session.commit()
        statuses = self._last_run_statuses([conversation.id])
        return self._serialize_conversation(
            conversation, app, last_run_status=statuses.get(conversation.id, "idle"),
        )

    def delete_conversation(self, conversation_id: UUID, account: Account) -> None:
        """清理可恢复运行资源后软删除会话，清理失败时不提交删除标记。"""
        # 1.先校验归属，再执行可能涉及外部资源的清理。
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
        # 2.清理成功后保存删除标记，后续列表和入口都不再返回该会话。
        conversation.is_deleted = True
        self.db.session.commit()

    def _visible_query(self, account: Account):
        """构建同账号可见的任务会话查询。

        ``owner_account_id`` 是新数据的稳定归属；created_by 和 EndUser
        条件用于兼容历史 WebApp 会话，待业务侧完成数据回填后仍可保留。
        """
        legacy_end_users = select(EndUser.id).where(
            EndUser.external_user_id == account.id
        )
        # 新数据只认稳定的 owner；只有尚未回填的会话才允许按旧创建者关系判断。
        legacy_webapp_owner = and_(
            Conversation.invoke_from == InvokeFrom.WEB_APP,
            Conversation.created_by.in_(legacy_end_users),
        )
        legacy_creator = or_(Conversation.created_by == account.id, legacy_webapp_owner)
        legacy_ownership = and_(Conversation.owner_account_id.is_(None), legacy_creator)
        ownership = or_(Conversation.owner_account_id == account.id, legacy_ownership)
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
        """历史数据没有时间时沿用接口约定返回 0。"""
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
