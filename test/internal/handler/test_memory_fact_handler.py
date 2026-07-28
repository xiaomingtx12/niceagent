"""MemoryFactHandler contract tests."""

from unittest.mock import ANY, MagicMock
from uuid import uuid4

import pytest
from flask import Flask
from flask_login import LoginManager, UserMixin, login_user

from internal.handler.memory_fact_handler import MemoryFactHandler


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
    end_user = MagicMock(id=uuid4())
    handler = MemoryFactHandler(
        memory_fact_service=MagicMock(),
        web_app_service=MagicMock(),
        app_service=MagicMock(),
    )
    handler.web_app_service.get_web_app.return_value = app
    handler.web_app_service.resolve_web_app_end_user.return_value = end_user
    handler.app_service.get_app.return_value = app
    return flask_app, account, app, end_user, handler


def _call(web, path, method, view, *, json=None, **kwargs):
    flask_app, account, *_ = web
    with flask_app.test_request_context(path, method=method, json=json):
        login_user(account)
        return view(**kwargs)


def _data(response):
    return response[0].get_json()["data"]


def test_list_resolves_server_side_end_user_and_validates_pagination(web):
    _, _, app, end_user, handler = web
    fact = MagicMock()
    fact.to_dict.return_value = {"id": "fact-1", "content": "偏好中文"}
    handler.memory_fact_service.list_end_user_facts.return_value = [fact]

    response = _call(
        web,
        "/web-apps/token/memories?sort=importance&limit=25&offset=5",
        "GET",
        handler.list_my_memories,
        token="token",
    )

    handler.memory_fact_service.list_end_user_facts.assert_called_once_with(
        account_id=app.account_id,
        app_id=app.id,
        end_user_id=end_user.id,
        keyword=None,
        sort="importance",
        limit=25,
        offset=5,
    )
    assert _data(response)["items"] == [{"id": "fact-1", "content": "偏好中文"}]

    invalid = _call(
        web,
        "/web-apps/token/memories?limit=bad",
        "GET",
        handler.list_my_memories,
        token="token",
    )
    assert "pagination" in _data(invalid)


def test_manual_create_validates_importance(web):
    _, _, app, end_user, handler = web
    fact = MagicMock()
    fact.to_dict.return_value = {"id": "new"}
    handler.memory_fact_service.create_end_user_fact.return_value = fact

    response = _call(
        web,
        "/web-apps/token/memories",
        "POST",
        handler.create_my_memory,
        token="token",
        json={"content": "  长期使用中文  ", "importance": 0.8},
    )

    handler.memory_fact_service.create_end_user_fact.assert_called_once_with(
        account_id=app.account_id,
        app_id=app.id,
        end_user_id=end_user.id,
        content="  长期使用中文  ",
        importance=0.8,
    )
    assert _data(response) == {"id": "new"}

    invalid = _call(
        web,
        "/web-apps/token/memories",
        "POST",
        handler.create_my_memory,
        token="token",
        json={"content": "x", "importance": 2},
    )
    assert "importance" in _data(invalid)


def test_batch_delete_rejects_invalid_time_and_scopes_to_end_user(web):
    _, _, app, end_user, handler = web
    handler.memory_fact_service.batch_delete_end_user_facts.return_value = 2
    fact_ids = [str(uuid4()), str(uuid4())]

    response = _call(
        web,
        "/web-apps/token/memories/batch-delete",
        "POST",
        handler.batch_delete_my_memories,
        token="token",
        json={"fact_ids": fact_ids},
    )
    handler.memory_fact_service.batch_delete_end_user_facts.assert_called_once_with(
        account_id=app.account_id,
        app_id=app.id,
        end_user_id=end_user.id,
        fact_ids=fact_ids,
        before=None,
        after=None,
    )
    assert _data(response) == {"deleted": 2}

    invalid = _call(
        web,
        "/web-apps/token/memories/batch-delete",
        "POST",
        handler.batch_delete_my_memories,
        token="token",
        json={"before": "not-a-date"},
    )
    assert "before" in _data(invalid)


def test_creator_memory_endpoint_is_read_only_inspection(web):
    _, account, app, _, handler = web
    fact = MagicMock()
    fact.to_dict.return_value = {"id": "creator-visible"}
    handler.memory_fact_service.list_creator_facts.return_value = [fact]

    response = _call(
        web,
        f"/apps/{app.id}/memories?keyword=java",
        "GET",
        handler.list_app_memories,
        app_id=app.id,
    )

    handler.app_service.get_app.assert_called_once_with(app.id, ANY)
    handler.memory_fact_service.list_creator_facts.assert_called_once_with(
        account_id=app.account_id,
        app_id=app.id,
        end_user_id_filter=None,
        keyword="java",
    )
    assert _data(response)["items"] == [{"id": "creator-visible"}]
