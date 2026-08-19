"""Creator and end-user Workspace HTTP endpoints."""

from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from uuid import UUID

from flask import request, send_file
from flask_login import current_user, login_required
from injector import inject

from internal.exception import FailException
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
        if (
            not isinstance(payload.get("conversation_id"), str)
            or not isinstance(payload.get("path"), str)
            or not isinstance(payload.get("content"), str)
        ):
            return validate_error_json(
                {"path": ["conversation_id、path 和 content 必须是字符串"]}
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
            conversation_id=request.args.get("conversation_id"),
        )
        return success_json(row.to_dict(include_content=True))

    @login_required
    def delete_app_file(self, app_id: UUID):
        app = self.app_service.get_app(app_id, current_user)
        payload = request.get_json(silent=True) or {}
        if (
            not isinstance(payload.get("conversation_id"), str)
            or not isinstance(payload.get("path"), str)
            or not payload["path"].strip()
        ):
            return validate_error_json(
                {"path": ["conversation_id 和 path 不能为空"]}
            )
        self.workspace_file_service.soft_delete(
            account_id=app.account_id,
            app_id=app.id,
            path=payload["path"],
            conversation_id=payload.get("conversation_id"),
        )
        return success_message("Workspace 文件已删除")

    @login_required
    def list_all_web_app_files(self, token: str):
        """List all Workspace files owned by the current WebApp user."""
        app = self.web_app_service.get_web_app(token)
        end_user = self.web_app_service.resolve_web_app_end_user(
            app=app,
            account=current_user,
        )
        rows = self.workspace_file_service.list_end_user_all_files(
            account_id=app.account_id,
            app_id=app.id,
            owner_ids=[end_user.id, current_user.id],
            query=request.args.get("query"),
        )
        names = self.workspace_file_service.conversation_names(
            app_id=app.id,
            conversation_ids=[
                row.conversation_id for row in rows if row.conversation_id
            ],
        )
        items = []
        for row in rows:
            item = row.to_dict()
            item["conversation_name"] = names.get(
                str(row.conversation_id),
                "未命名会话",
            )
            items.append(item)
        return success_json({"items": items})

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
            conversation_id=conversation.id,
            end_user_conversation_id=conversation.id,
        )
        return success_json(row.to_dict(include_content=True))

    @login_required
    def download_conversation_file(self, token: str, conversation_id: UUID):
        """下载当前终端用户会话内的 Workspace 文件。"""
        app, _, conversation = self.web_app_service.get_owned_web_conversation(
            token=token,
            conversation_id=conversation_id,
            account=current_user,
        )
        row = self.workspace_file_service.read_owned(
            account_id=app.account_id,
            app_id=app.id,
            path=request.args.get("path", ""),
            conversation_id=conversation.id,
            end_user_conversation_id=conversation.id,
        )
        if row.content is None:
            raise FailException("Workspace 文件内容暂不可下载")
        filename = PurePosixPath(row.path).name or "workspace-file"
        return send_file(
            BytesIO(row.content),
            mimetype=row.mime_type or "application/octet-stream",
            as_attachment=True,
            download_name=filename,
            max_age=0,
        )

__all__ = ["WorkspaceFileHandler"]
