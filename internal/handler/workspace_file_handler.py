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
    WorkspaceFileWithConversationResp,
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
        """列出创建者指定会话的文件。"""
        req = ListWorkspaceFilesReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.app_service.get_app(app_id, current_user)
        rows = self.workspace_file_service.list_creator_files(
            account_id=app.account_id,
            app_id=app.id,
            conversation_id=req.conversation_id.data,
            prefix=req.path_prefix.data,
        )
        return success_json({"items": WorkspaceFileResp(many=True).dump(rows)})

    @login_required
    def write_app_file(self, app_id: UUID):
        """写入创建者会话中的文本文件。"""
        req = WriteWorkspaceFileReq()
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.app_service.get_app(app_id, current_user)
        row = self.workspace_file_service.write_text(
            account_id=app.account_id,
            app_id=app.id,
            conversation_id=req.conversation_id.data,
            path=req.path.data,
            content=req.content.data,
            created_by=current_user.id,
            mime_type=req.mime_type.data or "text/plain",
        )
        return success_json(WorkspaceFileResp().dump(row))

    @login_required
    def read_app_file(self, app_id: UUID):
        """预览创建者会话中的文件。"""
        req = ReadWorkspaceFileReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.app_service.get_app(app_id, current_user)
        row = self.workspace_file_service.read_owned(
            account_id=app.account_id,
            app_id=app.id,
            path=req.path.data,
            conversation_id=req.conversation_id.data,
        )
        preview = self.workspace_file_service.preview(row)
        return success_json(WorkspaceFilePreviewResp().dump(preview))

    @login_required
    def download_app_file(self, app_id: UUID):
        """下载创建者会话中的文件。"""
        req = ReadWorkspaceFileReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.app_service.get_app(app_id, current_user)
        row = self.workspace_file_service.read_owned(
            account_id=app.account_id,
            app_id=app.id,
            path=req.path.data,
            conversation_id=req.conversation_id.data,
        )
        return redirect(self.workspace_file_service.get_download_url(row), code=302)

    @login_required
    def delete_app_file(self, app_id: UUID):
        """删除创建者会话中的文件。"""
        req = DeleteWorkspaceFileReq()
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.app_service.get_app(app_id, current_user)
        self.workspace_file_service.soft_delete(
            account_id=app.account_id,
            app_id=app.id,
            path=req.path.data,
            conversation_id=req.conversation_id.data,
        )
        return success_message("Workspace 文件已删除")

    @login_required
    def list_all_web_app_files(self, token: str):
        """列出当前终端用户拥有的全部会话文件。"""
        req = SearchWorkspaceFilesReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.web_app_service.get_web_app(token)
        end_user = self.web_app_service.resolve_web_app_end_user(
            app=app,
            account=current_user,
        )
        rows = self.workspace_file_service.list_end_user_all_files(
            account_id=app.account_id,
            app_id=app.id,
            owner_ids=[end_user.id, current_user.id],
            query=req.query.data,
        )
        names = self.workspace_file_service.conversation_names(
            app_id=app.id,
            conversation_ids=[row.conversation_id for row in rows],
        )
        items = WorkspaceFileWithConversationResp(many=True).dump(
            [
                (
                    row,
                    names.get(str(row.conversation_id), "未命名会话"),
                )
                for row in rows
            ]
        )
        return success_json({"items": items})

    @login_required
    def read_conversation_file(self, token: str, conversation_id: UUID):
        """预览终端用户当前会话中的文件。"""
        req = ConversationWorkspaceFileReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app, _, conversation = self.web_app_service.get_owned_web_conversation(
            token=token,
            conversation_id=conversation_id,
            account=current_user,
        )
        row = self.workspace_file_service.read_owned(
            account_id=app.account_id,
            app_id=app.id,
            path=req.path.data,
            conversation_id=conversation.id,
            end_user_conversation_id=conversation.id,
        )
        return success_json(
            WorkspaceFilePreviewResp().dump(self.workspace_file_service.preview(row))
        )

    @login_required
    def download_conversation_file(self, token: str, conversation_id: UUID):
        """下载终端用户当前会话中的文件。"""
        req = ConversationWorkspaceFileReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app, _, conversation = self.web_app_service.get_owned_web_conversation(
            token=token,
            conversation_id=conversation_id,
            account=current_user,
        )
        row = self.workspace_file_service.read_owned(
            account_id=app.account_id,
            app_id=app.id,
            path=req.path.data,
            conversation_id=conversation.id,
            end_user_conversation_id=conversation.id,
        )
        return redirect(self.workspace_file_service.get_download_url(row), code=302)


__all__ = ["WorkspaceFileHandler"]
