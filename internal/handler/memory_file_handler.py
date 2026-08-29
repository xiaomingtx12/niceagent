"""长期记忆文件 HTTP 适配器。"""

from dataclasses import dataclass
from uuid import UUID

from flask import request
from flask_login import current_user, login_required
from injector import inject

from internal.schema.memory_file_schema import (
    DeleteMemoryFileReq,
    ListMemoryFilesResp,
    MemoryFileContentResp,
    ReadMemoryFileReq,
    WriteMemoryFileReq,
)
from internal.service import AppService, MemoryFileService, WebAppService
from pkg.response import success_json, success_message, validate_error_json


@inject
@dataclass
class MemoryFileHandler:
    """解析身份和 Schema，并把记忆用例交给 Service。"""

    app_service: AppService
    web_app_service: WebAppService
    memory_file_service: MemoryFileService

    @login_required
    def list_app_memory_files(self, app_id: UUID):
        """列出当前创建者在草稿应用中的长期记忆。"""
        app = self.app_service.get_app(app_id, current_user)
        rows = self.memory_file_service.list_files(
            account_id=app.account_id,
            app_id=app.id,
            principal_id=current_user.id,
        )
        return success_json(ListMemoryFilesResp().dump({"items": rows}))

    @login_required
    def read_app_memory_file(self, app_id: UUID):
        """读取当前创建者的一个长期记忆文件。"""
        req = ReadMemoryFileReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.app_service.get_app(app_id, current_user)
        row = self.memory_file_service.read_file(
            account_id=app.account_id,
            app_id=app.id,
            principal_id=current_user.id,
            path=req.path.data,
        )
        return success_json(MemoryFileContentResp().dump(row))

    @login_required
    def write_app_memory_file(self, app_id: UUID):
        """写入当前创建者的一个长期记忆文本。"""
        req = WriteMemoryFileReq()
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.app_service.get_app(app_id, current_user)
        row = self.memory_file_service.write_file(
            account_id=app.account_id,
            app_id=app.id,
            principal_id=current_user.id,
            path=req.path.data,
            content=req.content.data,
        )
        return success_json(MemoryFileContentResp().dump(row))

    @login_required
    def delete_app_memory_file(self, app_id: UUID):
        """删除当前创建者的一个长期记忆文件。"""
        req = DeleteMemoryFileReq()
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.app_service.get_app(app_id, current_user)
        self.memory_file_service.delete_file(
            account_id=app.account_id,
            app_id=app.id,
            principal_id=current_user.id,
            path=req.path.data,
        )
        return success_message("记忆文件已删除")

    @login_required
    def list_web_app_memory_files(self, token: str):
        """列出当前 WebApp 主体的长期记忆。"""
        app = self.web_app_service.get_web_app(token)
        principal = self.web_app_service.resolve_web_app_end_user(
            app=app,
            account=current_user,
        )
        rows = self.memory_file_service.list_files(
            account_id=app.account_id,
            app_id=app.id,
            principal_id=principal.id,
        )
        return success_json(ListMemoryFilesResp().dump({"items": rows}))

    @login_required
    def read_web_app_memory_file(self, token: str):
        """读取当前 WebApp 主体的一个长期记忆文件。"""
        req = ReadMemoryFileReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.web_app_service.get_web_app(token)
        principal = self.web_app_service.resolve_web_app_end_user(
            app=app,
            account=current_user,
        )
        row = self.memory_file_service.read_file(
            account_id=app.account_id,
            app_id=app.id,
            principal_id=principal.id,
            path=req.path.data,
        )
        return success_json(MemoryFileContentResp().dump(row))

    @login_required
    def write_web_app_memory_file(self, token: str):
        """写入当前 WebApp 主体的一个长期记忆文本。"""
        req = WriteMemoryFileReq()
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.web_app_service.get_web_app(token)
        principal = self.web_app_service.resolve_web_app_end_user(
            app=app,
            account=current_user,
        )
        row = self.memory_file_service.write_file(
            account_id=app.account_id,
            app_id=app.id,
            principal_id=principal.id,
            path=req.path.data,
            content=req.content.data,
        )
        return success_json(MemoryFileContentResp().dump(row))

    @login_required
    def delete_web_app_memory_file(self, token: str):
        """删除当前 WebApp 主体的一个长期记忆文件。"""
        req = DeleteMemoryFileReq()
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.web_app_service.get_web_app(token)
        principal = self.web_app_service.resolve_web_app_end_user(
            app=app,
            account=current_user,
        )
        self.memory_file_service.delete_file(
            account_id=app.account_id,
            app_id=app.id,
            principal_id=principal.id,
            path=req.path.data,
        )
        return success_message("记忆文件已删除")


__all__ = ["MemoryFileHandler"]
