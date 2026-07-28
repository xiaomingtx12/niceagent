"""Workspace creator API contract tests."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from flask import Flask
from flask_login import LoginManager, UserMixin, login_user

from internal.handler.workspace_file_handler import WorkspaceFileHandler


class _Account(UserMixin):
    def __init__(self):
        self.id = uuid4()

    def get_id(self):
        return str(self.id)


@pytest.fixture
def web():
    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test"
    login_manager = LoginManager(flask_app)
    account = _Account()

    @login_manager.user_loader
    def _load_user(_user_id):
        return account
    app = MagicMock(id=uuid4(), account_id=uuid4())
    handler = WorkspaceFileHandler(
        db=MagicMock(),
        app_service=MagicMock(),
        web_app_service=MagicMock(),
        workspace_file_service=MagicMock(),
    )
    handler.app_service.get_app.return_value = app
    return flask_app, account, app, handler


def _call(web, path, method, view, *, json=None, **kwargs):
    flask_app, account, *_ = web
    with flask_app.test_request_context(path, method=method, json=json):
        login_user(account)
        return view(**kwargs)


def test_creator_write_uses_server_owned_app_scope(web):
    _, account, app, handler = web
    row = MagicMock()
    row.to_dict.return_value = {"path": "seed/guide.md", "content": "hello"}
    handler.workspace_file_service.write_text.return_value = row

    response = _call(
        web,
        f"/apps/{app.id}/workspace/files",
        "POST",
        handler.write_app_file,
        app_id=app.id,
        json={
            "path": "seed/guide.md",
            "content": "hello",
            "mime_type": "text/markdown",
        },
    )

    handler.workspace_file_service.write_text.assert_called_once_with(
        account_id=app.account_id,
        app_id=app.id,
        conversation_id=None,
        path="seed/guide.md",
        content="hello",
        created_by=account.id,
        mime_type="text/markdown",
    )
    assert response[0].get_json()["data"]["path"] == "seed/guide.md"


def test_creator_delete_requires_non_empty_path(web):
    _, _, app, handler = web
    response = _call(
        web,
        f"/apps/{app.id}/workspace/file",
        "DELETE",
        handler.delete_app_file,
        app_id=app.id,
        json={},
    )
    assert "path" in response[0].get_json()["data"]
    handler.workspace_file_service.soft_delete.assert_not_called()
