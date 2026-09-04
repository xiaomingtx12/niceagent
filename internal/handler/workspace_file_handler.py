"""Workspace 文件 HTTP 适配器。"""

from dataclasses import dataclass
from uuid import UUID

from flask import redirect, request
from flask_login import current_user, login_required
from injector import inject

from internal.schema.workspace_file_schema import (
    ConversationWorkspaceFileReq,
    DeleteWorkspaceFileReq,
    ListWorkspaceFilesReq,
    ReadWorkspaceFileReq,
    SearchWorkspaceFilesReq,
    WorkspaceFilePreviewResp,
    WorkspaceFileResp,
    WriteWorkspaceFileReq,
)
from internal.service import AppService, WebAppService, WorkspaceFileService
from pkg.response import success_json, success_message, validate_error_json


@inject
@dataclass
class WorkspaceFileHandler:
    """解析 Workspace 请求并交由 Service 执行业务。"""

    app_service: AppService
    web_app_service: WebAppService
    workspace_file_service: WorkspaceFileService

    @login_required
    def list_app_files(self, app_id: UUID):
        """列出当前用户在该 Agent 下的共享 Workspace 文件。"""
        req = ListWorkspaceFilesReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.app_service.get_app(app_id, current_user)
        rows = self.workspace_file_service.list_files(
            account_id=app.account_id,
            app_id=app.id,
            prefix=req.path_prefix.data,
        )
        return success_json({"items": WorkspaceFileResp(many=True).dump(rows)})

    @login_required
    def write_app_file(self, app_id: UUID):
        """写入当前用户在该 Agent 下的共享 Workspace 文件。"""
        req = WriteWorkspaceFileReq()
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.app_service.get_app(app_id, current_user)
        row = self.workspace_file_service.write_text(
            account_id=app.account_id,
            app_id=app.id,
            path=req.path.data,
            content=req.content.data,
            created_by=current_user.id,
            mime_type=req.mime_type.data or "text/plain",
        )
        return success_json(WorkspaceFileResp().dump(row))

    @login_required
    def read_app_file(self, app_id: UUID):
        """预览当前用户在该 Agent 下的共享 Workspace 文件。"""
        req = ReadWorkspaceFileReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.app_service.get_app(app_id, current_user)
        preview = self.workspace_file_service.preview(
            account_id=app.account_id,
            app_id=app.id,
            path=req.path.data,
        )
        return success_json(WorkspaceFilePreviewResp().dump(preview))

    @login_required
    def download_app_file(self, app_id: UUID):
        """下载当前用户在该 Agent 下的共享 Workspace 文件。"""
        req = ReadWorkspaceFileReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.app_service.get_app(app_id, current_user)
        url = self.workspace_file_service.get_download_url(
            account_id=app.account_id,
            app_id=app.id,
            path=req.path.data,
        )
        return redirect(url, code=302)

    @login_required
    def delete_app_file(self, app_id: UUID):
        """删除当前用户在该 Agent 下的共享 Workspace 文件。"""
        req = DeleteWorkspaceFileReq()
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.app_service.get_app(app_id, current_user)
        self.workspace_file_service.delete(
            account_id=app.account_id,
            app_id=app.id,
            path=req.path.data,
        )
        return success_message("Workspace 文件已删除")

    @login_required
    def list_all_web_app_files(self, token: str):
        """列出当前用户在该 Agent 下的共享 Workspace 文件。"""
        req = SearchWorkspaceFilesReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.web_app_service.get_web_app(token)
        rows = self.workspace_file_service.search_files(
            account_id=current_user.id,
            app_id=app.id,
            query=req.query.data,
        )
        return success_json({"items": WorkspaceFileResp(many=True).dump(rows)})

    @login_required
    def read_web_app_file(self, token: str):
        """预览当前用户在 WebApp Agent 下的共享 Workspace 文件。"""
        req = ReadWorkspaceFileReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.web_app_service.get_web_app(token)
        preview = self.workspace_file_service.preview(
            account_id=current_user.id,
            app_id=app.id,
            path=req.path.data,
        )
        return success_json(WorkspaceFilePreviewResp().dump(preview))

    @login_required
    def download_web_app_file(self, token: str):
        """下载当前用户在 WebApp Agent 下的共享 Workspace 文件。"""
        req = ReadWorkspaceFileReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.web_app_service.get_web_app(token)
        url = self.workspace_file_service.get_download_url(
            account_id=current_user.id,
            app_id=app.id,
            path=req.path.data,
        )
        return redirect(url, code=302)

    @login_required
    def read_conversation_file(self, token: str, conversation_id: UUID):
        """兼容按会话路由预览当前用户的共享 Workspace 文件。"""
        req = ConversationWorkspaceFileReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app, _, _ = self.web_app_service.get_owned_web_conversation(
            token=token,
            conversation_id=conversation_id,
            account=current_user,
        )
        preview = self.workspace_file_service.preview(
            account_id=current_user.id,
            app_id=app.id,
            path=req.path.data,
        )
        return success_json(WorkspaceFilePreviewResp().dump(preview))

    @login_required
    def download_conversation_file(self, token: str, conversation_id: UUID):
        """兼容按会话路由下载当前用户的共享 Workspace 文件。"""
        req = ConversationWorkspaceFileReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app, _, _ = self.web_app_service.get_owned_web_conversation(
            token=token,
            conversation_id=conversation_id,
            account=current_user,
        )
        url = self.workspace_file_service.get_download_url(
            account_id=current_user.id,
            app_id=app.id,
            path=req.path.data,
        )
        return redirect(url, code=302)


__all__ = ["WorkspaceFileHandler"]
