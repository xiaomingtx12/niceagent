"""End-user owned memory API and creator read-only inspection."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from flask import request
from flask_login import current_user, login_required
from injector import inject

from internal.service import (
    AppService,
    MemoryFactService,
    WebAppService,
)
from pkg.response import success_json, success_message, validate_error_json


@inject
@dataclass
class MemoryFactHandler:
    memory_fact_service: MemoryFactService
    web_app_service: WebAppService
    app_service: AppService

    @login_required
    def list_my_memories(self, token: str):
        app, end_user = self._identity(token)
        try:
            limit = int(request.args.get("limit", 50))
            offset = int(request.args.get("offset", 0))
        except (TypeError, ValueError):
            return validate_error_json(
                {"pagination": ["limit 和 offset 必须是整数"]}
            )
        if not 1 <= limit <= 200 or offset < 0:
            return validate_error_json(
                {
                    "pagination": [
                        "limit 必须在 1 到 200 之间，offset 不能小于 0"
                    ]
                }
            )
        sort = request.args.get("sort", "updated_at")
        if sort not in {"updated_at", "importance"}:
            return validate_error_json(
                {"sort": ["sort 只能是 updated_at 或 importance"]}
            )
        rows = self.memory_fact_service.list_end_user_facts(
            account_id=app.account_id,
            app_id=app.id,
            end_user_id=end_user.id,
            keyword=request.args.get("keyword"),
            sort=sort,
            limit=limit,
            offset=offset,
        )
        return success_json({"items": [row.to_dict() for row in rows]})

    @login_required
    def create_my_memory(self, token: str):
        app, end_user = self._identity(token)
        payload = request.get_json(silent=True) or {}
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            return validate_error_json({"content": ["记忆内容不能为空"]})
        importance = self._parse_importance(payload.get("importance", 0.7))
        if importance is None:
            return validate_error_json(
                {"importance": ["importance 必须是 0 到 1 之间的数字"]}
            )
        row = self.memory_fact_service.create_end_user_fact(
            account_id=app.account_id,
            app_id=app.id,
            end_user_id=end_user.id,
            content=content,
            importance=importance,
        )
        return success_json(row.to_dict())

    @login_required
    def update_my_memory(self, token: str, fact_id: UUID):
        app, end_user = self._identity(token)
        payload = request.get_json(silent=True) or {}
        content = payload.get("content")
        if not isinstance(content, str) or not content.strip():
            return validate_error_json({"content": ["记忆内容不能为空"]})
        importance = (
            self._parse_importance(payload["importance"])
            if payload.get("importance") is not None
            else None
        )
        if payload.get("importance") is not None and importance is None:
            return validate_error_json(
                {"importance": ["importance 必须是 0 到 1 之间的数字"]}
            )
        row = self.memory_fact_service.update_end_user_fact(
            account_id=app.account_id,
            app_id=app.id,
            end_user_id=end_user.id,
            fact_id=fact_id,
            content=content,
            importance=importance,
        )
        return success_json(row.to_dict())

    @login_required
    def delete_my_memory(self, token: str, fact_id: UUID):
        app, end_user = self._identity(token)
        self.memory_fact_service.soft_delete_end_user_fact(
            account_id=app.account_id,
            app_id=app.id,
            end_user_id=end_user.id,
            fact_id=fact_id,
        )
        return success_message("记忆已删除")

    @login_required
    def batch_delete_my_memories(self, token: str):
        app, end_user = self._identity(token)
        payload = request.get_json(silent=True) or {}
        fact_ids = payload.get("fact_ids")
        if fact_ids is not None and (
            not isinstance(fact_ids, list)
            or not all(isinstance(item, str) for item in fact_ids)
        ):
            return validate_error_json(
                {"fact_ids": ["fact_ids 必须是 UUID 字符串数组"]}
            )
        before = self._parse_datetime(payload.get("before"))
        after = self._parse_datetime(payload.get("after"))
        if payload.get("before") and before is None:
            return validate_error_json(
                {"before": ["before 必须是 ISO 8601 时间"]}
            )
        if payload.get("after") and after is None:
            return validate_error_json(
                {"after": ["after 必须是 ISO 8601 时间"]}
            )
        if before and after and after > before:
            return validate_error_json(
                {"time_range": ["after 不能晚于 before"]}
            )
        count = self.memory_fact_service.batch_delete_end_user_facts(
            account_id=app.account_id,
            app_id=app.id,
            end_user_id=end_user.id,
            fact_ids=fact_ids,
            before=before,
            after=after,
        )
        return success_json({"deleted": count})

    @login_required
    def list_app_memories(self, app_id: UUID):
        app = self.app_service.get_app(app_id, current_user)
        rows = self.memory_fact_service.list_creator_facts(
            account_id=app.account_id,
            app_id=app.id,
            end_user_id_filter=request.args.get("end_user_id"),
            keyword=request.args.get("keyword"),
        )
        return success_json({"items": [row.to_dict() for row in rows]})

    def _identity(self, token: str):
        app = self.web_app_service.get_web_app(token)
        end_user = self.web_app_service.resolve_web_app_end_user(
            app=app,
            account=current_user,
        )
        return app, end_user

    @staticmethod
    def _parse_datetime(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _parse_importance(value):
        if isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if 0 <= parsed <= 1 else None


__all__ = ["MemoryFactHandler"]
