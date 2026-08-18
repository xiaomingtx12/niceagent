"""Creator and end-user Workspace HTTP endpoints."""

from dataclasses import dataclass
from uuid import UUID

from flask import request
from flask_login import current_user, login_required
from injector import inject

from internal.service import AppService, WebAppService, WorkspaceFileService
from pkg.response import success_json, success_message, validate_error_json


@inject
@dataclass
class WorkspaceFileHandler:
    app_service: AppService
    web_app_service: WebAppService
    workspace_file_service: WorkspaceFileService

    @login_required
    def list_app_files(self, app_id: UUID):
        app = self.app_service.get_app(app_id, current_user)
        rows = self.workspace_file_service.list_creator_files(
            account_id=app.account_id,
            app_id=app.id,
            conversation_id=request.args.get("conversation_id"),
            prefix=request.args.get("path_prefix"),
        )
        return success_json({"items": [row.to_dict() for row in rows]})

    @login_required
    def write_app_file(self, app_id: UUID):
        app = self.app_service.get_app(app_id, current_user)
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload.get("path"), str) or not isinstance(
            payload.get("content"),
            str,
        ):
            return validate_error_json(
                {"path": ["path 和 content 必须是字符串"]}
            )
        row = self.workspace_file_service.write_text(
            account_id=app.account_id,
            app_id=app.id,
            conversation_id=payload.get("conversation_id"),
            path=payload["path"],
            content=payload["content"],
            created_by=current_user.id,
            mime_type=payload.get("mime_type", "text/plain"),
        )
        return success_json(row.to_dict(include_content=True))

    @login_required
    def read_app_file(self, app_id: UUID):
        app = self.app_service.get_app(app_id, current_user)
        path = request.args.get("path", "")
        row = self.workspace_file_service.read_owned(
            account_id=app.account_id,
            app_id=app.id,
            path=path,
        )
        return success_json(row.to_dict(include_content=True))

    @login_required
    def delete_app_file(self, app_id: UUID):
        app = self.app_service.get_app(app_id, current_user)
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload.get("path"), str) or not payload["path"].strip():
            return validate_error_json({"path": ["path 不能为空"]})
        self.workspace_file_service.soft_delete(
            account_id=app.account_id,
            app_id=app.id,
            path=payload["path"],
        )
        return success_message("Workspace 文件已删除")

    @login_required
    def list_conversation_files(self, token: str, conversation_id: UUID):
        app, end_user, conversation = self.web_app_service.get_owned_web_conversation(
            token=token,
            conversation_id=conversation_id,
            account=current_user,
        )
        rows = self.workspace_file_service.list_end_user_files(
            account_id=app.account_id,
            app_id=app.id,
            conversation_id=conversation.id,
            prefix=request.args.get("path_prefix"),
        )
        return success_json({"items": [row.to_dict() for row in rows]})

    @login_required
    def read_conversation_file(self, token: str, conversation_id: UUID):
        app, _, conversation = self.web_app_service.get_owned_web_conversation(
            token=token,
            conversation_id=conversation_id,
            account=current_user,
        )
        row = self.workspace_file_service.read_owned(
            account_id=app.account_id,
            app_id=app.id,
            path=request.args.get("path", ""),
            end_user_conversation_id=conversation.id,
        )
        return success_json(row.to_dict(include_content=True))

__all__ = ["WorkspaceFileHandler"]
