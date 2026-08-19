"""Creator and end-user long-term memory file endpoints."""

from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from uuid import UUID

from flask import request, send_file
from flask_login import current_user, login_required
from injector import inject

from internal.exception import FailException
from internal.service import AppService, MemoryFileService, WebAppService
from pkg.response import success_json, success_message, validate_error_json


@inject
@dataclass
class MemoryFileHandler:
    """Expose only the memory namespace owned by the authenticated user."""

    app_service: AppService
    web_app_service: WebAppService
    memory_file_service: MemoryFileService

    @login_required
    def list_app_memory_files(self, app_id: UUID):
        app = self.app_service.get_app(app_id, current_user)
        rows = self.memory_file_service.list_files(
            account_id=app.account_id,
            app_id=app.id,
        )
        return success_json({"items": [row.to_dict() for row in rows]})

    @login_required
    def read_app_memory_file(self, app_id: UUID):
        app = self.app_service.get_app(app_id, current_user)
        row = self.memory_file_service.read_file(
            account_id=app.account_id,
            app_id=app.id,
            end_user_id=None,
            path=request.args.get("path", ""),
        )
        return success_json(row.to_dict(include_content=True))

    @login_required
    def write_app_memory_file(self, app_id: UUID):
        app = self.app_service.get_app(app_id, current_user)
        payload = request.get_json(silent=True) or {}
        if (
            not isinstance(payload.get("path"), str)
            or not isinstance(payload.get("content"), str)
        ):
            return validate_error_json(
                {"path": ["path 和 content 必须是字符串"]}
            )
        row = self.memory_file_service.write_file(
            account_id=app.account_id,
            app_id=app.id,
            end_user_id=None,
            path=payload["path"],
            content=payload["content"],
        )
        return success_json(row.to_dict(include_content=True))

    @login_required
    def delete_app_memory_file(self, app_id: UUID):
        app = self.app_service.get_app(app_id, current_user)
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload.get("path"), str) or not payload["path"].strip():
            return validate_error_json({"path": ["path 不能为空"]})
        self.memory_file_service.delete_file(
            account_id=app.account_id,
            app_id=app.id,
            end_user_id=None,
            path=payload["path"],
        )
        return success_message("记忆文件已删除")

    @login_required
    def download_app_memory_file(self, app_id: UUID):
        app = self.app_service.get_app(app_id, current_user)
        row = self.memory_file_service.read_file(
            account_id=app.account_id,
            app_id=app.id,
            end_user_id=None,
            path=request.args.get("path", ""),
        )
        return self._download(row)

    @login_required
    def list_web_app_memory_files(self, token: str):
        app, end_user_id = self._web_app_memory_scope(token)
        rows = self.memory_file_service.list_files(
            account_id=app.account_id,
            app_id=app.id,
            end_user_id=end_user_id,
        )
        return success_json({"items": [row.to_dict() for row in rows]})

    @login_required
    def read_web_app_memory_file(self, token: str):
        app, end_user_id = self._web_app_memory_scope(token)
        row = self.memory_file_service.read_file(
            account_id=app.account_id,
            app_id=app.id,
            end_user_id=end_user_id,
            path=request.args.get("path", ""),
        )
        return success_json(row.to_dict(include_content=True))

    @login_required
    def write_web_app_memory_file(self, token: str):
        app, end_user_id = self._web_app_memory_scope(token)
        payload = request.get_json(silent=True) or {}
        if (
            not isinstance(payload.get("path"), str)
            or not isinstance(payload.get("content"), str)
        ):
            return validate_error_json(
                {"path": ["path 和 content 必须是字符串"]}
            )
        row = self.memory_file_service.write_file(
            account_id=app.account_id,
            app_id=app.id,
            end_user_id=end_user_id,
            path=payload["path"],
            content=payload["content"],
        )
        return success_json(row.to_dict(include_content=True))

    @login_required
    def delete_web_app_memory_file(self, token: str):
        app, end_user_id = self._web_app_memory_scope(token)
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload.get("path"), str) or not payload["path"].strip():
            return validate_error_json({"path": ["path 不能为空"]})
        self.memory_file_service.delete_file(
            account_id=app.account_id,
            app_id=app.id,
            end_user_id=end_user_id,
            path=payload["path"],
        )
        return success_message("记忆文件已删除")

    @login_required
    def download_web_app_memory_file(self, token: str):
        app, end_user_id = self._web_app_memory_scope(token)
        row = self.memory_file_service.read_file(
            account_id=app.account_id,
            app_id=app.id,
            end_user_id=end_user_id,
            path=request.args.get("path", ""),
        )
        return self._download(row)

    def _web_app_memory_scope(self, token: str):
        app = self.web_app_service.get_web_app(token)
        end_user = self.web_app_service.resolve_web_app_end_user(
            app=app,
            account=current_user,
        )
        return app, end_user.id

    @staticmethod
    def _download(row):
        if row.content_bytes is None:
            raise FailException("记忆文件内容暂不可下载")
        filename = PurePosixPath(row.path).name or "memory-file"
        return send_file(
            BytesIO(row.content_bytes),
            mimetype=row.mime_type,
            as_attachment=True,
            download_name=filename,
            max_age=0,
        )


__all__ = ["MemoryFileHandler"]
