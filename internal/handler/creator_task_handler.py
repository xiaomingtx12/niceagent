"""Creator 任务中心的 Agent 和会话历史接口。"""

from dataclasses import dataclass
from uuid import UUID

from flask import request
from flask_login import current_user, login_required
from injector import inject

from internal.service import CreatorTaskService
from pkg.paginator import CursorPaginatorReq
from pkg.response import success_json, success_message, validate_error_json


@inject
@dataclass
class CreatorTaskHandler:
    service: CreatorTaskService

    @login_required
    def get_agents(self):
        return success_json({"list": self.service.get_agents(current_user)})

    @login_required
    def get_agent(self, app_id: UUID):
        return success_json(self.service.get_agent(app_id, current_user))

    @login_required
    def get_conversations(self):
        query = CursorPaginatorReq(request.args)
        if not query.validate():
            return validate_error_json(query.errors)
        app_id = request.args.get("app_id")
        parsed_app_id = None
        if app_id:
            try:
                parsed_app_id = UUID(app_id)
            except ValueError:
                return validate_error_json({"app_id": ["app_id必须是合法的UUID"]})
        return success_json(
            self.service.get_conversations(
                current_user,
                app_id=parsed_app_id,
                before=query.before_uuid,
                page_size=query.page_size.data,
                search=request.args.get("search", ""),
            )
        )

    @login_required
    def get_conversation(self, conversation_id: UUID):
        return success_json(self.service.get_conversation(conversation_id, current_user))

    @login_required
    def update_conversation_name(self, conversation_id: UUID):
        body = request.get_json(silent=True)
        name = str(body.get("name", "")).strip() if isinstance(body, dict) else ""
        if not name:
            return validate_error_json({"name": ["会话名称不能为空"]})
        if len(name) > 255:
            return validate_error_json({"name": ["会话名称不能超过255个字符"]})
        return success_json(
            self.service.update_conversation(conversation_id, current_user, name=name)
        )

    @login_required
    def update_conversation_is_pinned(self, conversation_id: UUID):
        body = request.get_json(silent=True)
        is_pinned = body.get("is_pinned") if isinstance(body, dict) else None
        if not isinstance(is_pinned, bool):
            return validate_error_json({"is_pinned": ["is_pinned必须是布尔值"]})
        return success_json(
            self.service.update_conversation(
                conversation_id, current_user, is_pinned=is_pinned
            )
        )

    @login_required
    def delete_conversation(self, conversation_id: UUID):
        self.service.delete_conversation(conversation_id, current_user)
        return success_message("删除会话成功")


__all__ = ["CreatorTaskHandler"]
