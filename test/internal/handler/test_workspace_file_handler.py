"""Workspace HTTP endpoint contract tests."""

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


def test_creator_write_requires_current_conversation(web):
    _, account, app, handler = web
    row = MagicMock()
    row.to_dict.return_value = {
        "path": "/workspace/guide.md",
        "content": "hello",
    }
    handler.workspace_file_service.write_text.return_value = row
    conversation_id = uuid4()

    response = _call(
        web,
        f"/apps/{app.id}/workspace/files",
        "POST",
        handler.write_app_file,
        app_id=app.id,
        json={
            "conversation_id": str(conversation_id),
            "path": "/workspace/guide.md",
            "content": "hello",
            "mime_type": "text/markdown",
        },
    )

    handler.workspace_file_service.write_text.assert_called_once_with(
        account_id=app.account_id,
        app_id=app.id,
        conversation_id=str(conversation_id),
        path="/workspace/guide.md",
        content="hello",
        created_by=account.id,
        mime_type="text/markdown",
    )
    assert response[0].get_json()["data"]["path"] == "/workspace/guide.md"


def test_creator_delete_requires_conversation_and_path(web):
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


def test_end_user_download_returns_file_response(web):
    _, account, app, handler = web
    conversation = MagicMock(id=uuid4())
    handler.web_app_service.get_owned_web_conversation.return_value = (
        app,
        MagicMock(),
        conversation,
    )
    row = MagicMock(
        content=b"hello",
        mime_type="text/plain",
        path=f"{conversation.id}/guide.txt",
    )
    handler.workspace_file_service.read_owned.return_value = row

    response = _call(
        web,
        f"/web-apps/token/conversations/{conversation.id}/file/download",
        "GET",
        handler.download_conversation_file,
        token="token",
        conversation_id=conversation.id,
    )

    assert response.status_code == 200
    response.direct_passthrough = False
    assert response.data == b"hello"
    assert response.mimetype == "text/plain"
    handler.workspace_file_service.read_owned.assert_called_once_with(
        account_id=app.account_id,
        app_id=app.id,
        path="",
        conversation_id=conversation.id,
        end_user_conversation_id=conversation.id,
    )


def test_end_user_lists_all_workspace_files_with_conversation_name(web):
    _, account, app, handler = web
    end_user = MagicMock(id=uuid4())
    conversation_id = uuid4()
    row = MagicMock(conversation_id=conversation_id)
    row.to_dict.return_value = {
        "path": "/workspace/guide.md",
        "conversation_id": str(conversation_id),
    }
    handler.web_app_service.get_web_app.return_value = app
    handler.web_app_service.resolve_web_app_end_user.return_value = end_user
    handler.workspace_file_service.list_end_user_all_files.return_value = [row]
    handler.workspace_file_service.conversation_names.return_value = {
        str(conversation_id): "产品资料",
    }

    response = _call(
        web,
        f"/web-apps/token/workspace/files?query=guide",
        "GET",
        handler.list_all_web_app_files,
        token="token",
    )

    handler.workspace_file_service.list_end_user_all_files.assert_called_once_with(
        account_id=app.account_id,
        app_id=app.id,
        owner_ids=[end_user.id, account.id],
        query="guide",
    )
    assert response[0].get_json()["data"]["items"][0]["conversation_name"] == "产品资料"
