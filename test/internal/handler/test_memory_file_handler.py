from io import BytesIO
from unittest.mock import MagicMock
from uuid import uuid4

from flask import Flask
from flask_login import LoginManager, UserMixin, login_user
from langgraph.store.memory import InMemoryStore

from internal.handler.memory_file_handler import MemoryFileHandler
from internal.service.memory_file_service import MemoryFileService


class _Account(UserMixin):
    def __init__(self):
        self.id = uuid4()

    def get_id(self):
        return str(self.id)


def _web_context():
    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test"
    login_manager = LoginManager(flask_app)
    account = _Account()

    @login_manager.user_loader
    def _load_user(_user_id):
        return account

    return flask_app, account


def test_web_app_memory_download_uses_authenticated_end_user():
    flask_app, account = _web_context()
    app = MagicMock(id=uuid4(), account_id=uuid4())
    end_user = MagicMock(id=uuid4())
    store = InMemoryStore()
    service = MemoryFileService(store_provider=lambda: store)
    service.write_file(
        account_id=app.account_id,
        app_id=app.id,
        end_user_id=end_user.id,
        path="/memories/preferences.md",
        content="hello",
    )
    web_app_service = MagicMock()
    web_app_service.get_web_app.return_value = app
    web_app_service.resolve_web_app_end_user.return_value = end_user
    handler = MemoryFileHandler(
        app_service=MagicMock(),
        web_app_service=web_app_service,
        memory_file_service=service,
    )

    with flask_app.test_request_context(
        "/web-apps/token/memory/file/download",
        method="GET",
        query_string={"path": "/memories/preferences.md"},
    ):
        login_user(account)
        response = handler.download_web_app_memory_file("token")
        web_app_service.resolve_web_app_end_user.assert_called_once()
        call = web_app_service.resolve_web_app_end_user.call_args
        assert call.kwargs["app"] is app
        assert call.kwargs["account"].id == account.id

    assert response.status_code == 200
    response.direct_passthrough = False
    assert response.data == b"hello"
    assert response.mimetype == "text/markdown"
