"""Creator 任务中心的 Agent 和会话历史接口。"""

from dataclasses import dataclass
from uuid import UUID

from flask import request
from flask_login import current_user, login_required
from injector import inject
from werkzeug.datastructures import MultiDict

from internal.schema.creator_task_schema import (
    GetCreatorTaskConversationsReq,
    UpdateCreatorTaskConversationNameReq,
    UpdateCreatorTaskConversationPinnedReq,
)
from internal.service import CreatorTaskService
from pkg.response import success_json, success_message, validate_error_json


@inject
@dataclass
class CreatorTaskHandler:
    service: CreatorTaskService

    @login_required
    def create_conversation(self, app_id: UUID):
        return success_json(self.service.create_conversation(app_id, current_user))

    @login_required
    def get_agents(self):
        return success_json({"list": self.service.get_agents(current_user)})

    @login_required
    def get_agent(self, app_id: UUID):
        return success_json(self.service.get_agent(app_id, current_user))

    @login_required
    def get_conversations(self):
        """获取经过筛选和游标分页的任务会话。"""
        req = GetCreatorTaskConversationsReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        return success_json(
            self.service.get_conversations(
                current_user,
                app_id=UUID(req.app_id.data) if req.app_id.data else None,
                before=req.before_uuid,
                page_size=req.page_size.data,
                search=req.search.data or "",
            )
        )

    @login_required
    def get_conversation(self, conversation_id: UUID):
        return success_json(self.service.get_conversation(conversation_id, current_user))

    @login_required
    def update_conversation_name(self, conversation_id: UUID):
        """更新任务会话名称。"""
        payload = request.get_json(silent=True)
        req = UpdateCreatorTaskConversationNameReq(
            formdata=MultiDict(payload if isinstance(payload, dict) else {})
        )
        if not req.validate():
            return validate_error_json(req.errors)
        return success_json(
            self.service.update_conversation(
                conversation_id,
                current_user,
                name=req.name.data.strip(),
            )
        )

    @login_required
    def update_conversation_is_pinned(self, conversation_id: UUID):
        """更新任务会话置顶状态。"""
        payload = request.get_json(silent=True)
        formdata = MultiDict(payload if isinstance(payload, dict) else {})
        req = UpdateCreatorTaskConversationPinnedReq(formdata=formdata)
        if not req.validate():
            return validate_error_json(req.errors)
        return success_json(
            self.service.update_conversation(
                conversation_id,
                current_user,
                is_pinned=req.is_pinned.data,
            )
        )

    @login_required
    def delete_conversation(self, conversation_id: UUID):
        self.service.delete_conversation(conversation_id, current_user)
        return success_message("删除会话成功")


__all__ = ["CreatorTaskHandler"]
