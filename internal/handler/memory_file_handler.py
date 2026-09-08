"""长期记忆文件 HTTP 适配器。"""

from dataclasses import dataclass
from io import BytesIO
from uuid import UUID

from flask import request, send_file
from flask_login import current_user, login_required
from injector import inject

from internal.exception import ForbiddenException
from internal.schema.memory_file_schema import (
    DeleteMemoryFileReq,
    ListMemoryFilesResp,
    MemoryFileContentResp,
    ReadMemoryFileReq,
    WriteMemoryFileReq,
)
from internal.model import App
from internal.service import AppConfigService, AppService, MemoryFileService, WebAppService
from pkg.response import success_json, success_message, validate_error_json
from pydantic import ValidationError
from internal.core.agent_v2.entity.memory_entry import MemoryChange
from internal.schema.memory_entry_schema import (
    ListMemoryEntriesResp, MemoryEntryChangeReq, MemoryEntryChangeResp,
)


@inject
@dataclass
class MemoryFileHandler:
    """解析身份和 Schema，并把记忆用例交给 Service。"""

    app_service: AppService
    web_app_service: WebAppService
    memory_file_service: MemoryFileService
    app_config_service: AppConfigService

    @login_required
    def list_app_memory_entries(self, app_id: UUID):
        """创建者只能管理自己应用下的记忆条目。"""
        app = self.app_service.get_app(app_id, current_user)
        self._ensure_memory_enabled(app, draft=True)
        rows = self.memory_file_service.list_entries(account_id=current_user.id, app_id=app.id)
        return success_json(ListMemoryEntriesResp().dump({"items": rows}))

    @login_required
    def change_app_memory_entry(self, app_id: UUID):
        """复用应用所有权与能力开关校验。"""
        app = self.app_service.get_app(app_id, current_user)
        self._ensure_memory_enabled(app, draft=True)
        return self._change_entry(app)

    @login_required
    def list_web_app_memory_entries(self, token: str):
        """发布态仍以当前登录账号隔离记忆。"""
        app = self.web_app_service.get_web_app(token)
        self._ensure_memory_enabled(app, draft=False)
        rows = self.memory_file_service.list_entries(account_id=current_user.id, app_id=app.id)
        return success_json(ListMemoryEntriesResp().dump({"items": rows}))

    @login_required
    def change_web_app_memory_entry(self, token: str):
        """发布态用户不能通过请求体指定其他账号。"""
        app = self.web_app_service.get_web_app(token)
        self._ensure_memory_enabled(app, draft=False)
        return self._change_entry(app)

    def _change_entry(self, app: App):
        """协议校验留在 Handler，领域变更交给 Service。"""
        try:
            req = MemoryEntryChangeReq.model_validate(request.get_json(silent=True) or {})
        except ValidationError as exc:
            return validate_error_json({"memory": [error["msg"] for error in exc.errors()]})
        result = self.memory_file_service.change_entry(
            account_id=current_user.id, app_id=app.id,
            change=MemoryChange.model_validate(req.model_dump(exclude={"evidence"})),
            evidence=req.evidence,
        )
        return success_json(MemoryEntryChangeResp().dump(result))

    @login_required
    def list_app_memory_files(self, app_id: UUID):
        """列出当前创建者在草稿应用中的长期记忆。"""
        app = self.app_service.get_app(app_id, current_user)
        self._ensure_memory_enabled(app, draft=True)
        rows = self.memory_file_service.list_files(
            account_id=current_user.id,
            app_id=app.id,
        )
        return success_json(ListMemoryFilesResp().dump({"items": rows}))

    @login_required
    def read_app_memory_file(self, app_id: UUID):
        """读取当前创建者的一个长期记忆文件。"""
        req = ReadMemoryFileReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.app_service.get_app(app_id, current_user)
        self._ensure_memory_enabled(app, draft=True)
        row = self.memory_file_service.read_file(
            account_id=current_user.id,
            app_id=app.id,
            path=req.path.data,
        )
        return success_json(MemoryFileContentResp().dump(row))

    @login_required
    def download_app_memory_file(self, app_id: UUID):
        """下载当前创建者的一个长期记忆文件。"""
        req = ReadMemoryFileReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.app_service.get_app(app_id, current_user)
        self._ensure_memory_enabled(app, draft=True)
        row = self.memory_file_service.read_file(
            account_id=current_user.id,
            app_id=app.id,
            path=req.path.data,
        )
        return self._download(row.path, row.content)

    @login_required
    def write_app_memory_file(self, app_id: UUID):
        """写入当前创建者的一个长期记忆文本。"""
        req = WriteMemoryFileReq()
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.app_service.get_app(app_id, current_user)
        self._ensure_memory_enabled(app, draft=True)
        row = self.memory_file_service.write_file(
            account_id=current_user.id,
            app_id=app.id,
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
        self._ensure_memory_enabled(app, draft=True)
        self.memory_file_service.delete_file(
            account_id=current_user.id,
            app_id=app.id,
            path=req.path.data,
        )
        return success_message("记忆文件已删除")

    @login_required
    def list_web_app_memory_files(self, token: str):
        """列出当前 WebApp 主体的长期记忆。"""
        app = self.web_app_service.get_web_app(token)
        self._ensure_memory_enabled(app, draft=False)
        rows = self.memory_file_service.list_files(
            account_id=current_user.id,
            app_id=app.id,
        )
        return success_json(ListMemoryFilesResp().dump({"items": rows}))

    @login_required
    def read_web_app_memory_file(self, token: str):
        """读取当前 WebApp 主体的一个长期记忆文件。"""
        req = ReadMemoryFileReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.web_app_service.get_web_app(token)
        self._ensure_memory_enabled(app, draft=False)
        row = self.memory_file_service.read_file(
            account_id=current_user.id,
            app_id=app.id,
            path=req.path.data,
        )
        return success_json(MemoryFileContentResp().dump(row))

    @login_required
    def download_web_app_memory_file(self, token: str):
        """下载当前 WebApp 主体的一个长期记忆文件。"""
        req = ReadMemoryFileReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.web_app_service.get_web_app(token)
        self._ensure_memory_enabled(app, draft=False)
        row = self.memory_file_service.read_file(
            account_id=current_user.id,
            app_id=app.id,
            path=req.path.data,
        )
        return self._download(row.path, row.content)

    @login_required
    def write_web_app_memory_file(self, token: str):
        """写入当前 WebApp 主体的一个长期记忆文本。"""
        req = WriteMemoryFileReq()
        if not req.validate():
            return validate_error_json(req.errors)
        app = self.web_app_service.get_web_app(token)
        self._ensure_memory_enabled(app, draft=False)
        row = self.memory_file_service.write_file(
            account_id=current_user.id,
            app_id=app.id,
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
        self._ensure_memory_enabled(app, draft=False)
        self.memory_file_service.delete_file(
            account_id=current_user.id,
            app_id=app.id,
            path=req.path.data,
        )
        return success_message("记忆文件已删除")

    def _ensure_memory_enabled(self, app: App, *, draft: bool) -> None:
        """仅在应用显式启用长期记忆时开放记忆文件操作。"""
        app_config = (
            self.app_config_service.get_draft_app_config(app)
            if draft
            else self.app_config_service.get_app_config(app)
        )
        if not app_config.get("long_term_memory", {}).get("enable", False):
            raise ForbiddenException("该应用未启用长期记忆")

    @staticmethod
    def _download(path: str, content: str):
        """以 UTF-8 文本附件返回 Store 中的记忆文件。"""
        filename = path.rsplit("/", 1)[-1]
        return send_file(
            BytesIO(content.encode("utf-8")),
            as_attachment=True,
            download_name=filename,
            mimetype="text/plain",
        )


__all__ = ["MemoryFileHandler"]
