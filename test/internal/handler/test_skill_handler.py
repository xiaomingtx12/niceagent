"""Creator Skill HTTP contract tests."""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from flask import Flask
from flask_login import LoginManager, UserMixin, login_user

from internal.handler.skill_handler import SkillHandler
from internal.router.router import Router


class _DummyHandler:
    def __init__(self, prefix: str):
        self._prefix = prefix

    def __getattr__(self, name: str):
        def _view(*args, **kwargs):
            del args, kwargs
            return None

        _view.__name__ = f"{self._prefix}_{name}"
        setattr(self, name, _view)
        return _view


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
    handler = SkillHandler(app_service=MagicMock(), skill_service=MagicMock())
    handler.app_service.get_app.return_value = app
    return flask_app, account, app, handler


def _call(web, path, method, view, *, json=None, **kwargs):
    flask_app, account, *_ = web
    with flask_app.test_request_context(path, method=method, json=json):
        login_user(account)
        return view(**kwargs)


def test_list_can_include_all_versions(web):
    _, _, app, handler = web
    app.draft_app_config.skills = [{"name": "research"}]
    handler.skill_service.list_skills.return_value = [
        {"name": "research", "version": "1.0.0", "enabled": True}
    ]

    response = _call(
        web,
        f"/apps/{app.id}/skills?include_versions=true",
        "GET",
        handler.list_skills,
        app_id=app.id,
    )

    handler.skill_service.list_skills.assert_called_once_with(
        account_id=app.account_id,
        app_id=app.id,
        bindings=[{"name": "research"}],
        include_versions=True,
    )
    assert response[0].get_json()["data"]["items"][0]["version"] == "1.0.0"


def test_router_only_exposes_skill_catalog_get():
    flask_app = Flask(__name__)
    router = Router.__new__(Router)
    for name in Router.__dataclass_fields__:
        setattr(router, name, _DummyHandler(name))

    router.register_router(flask_app)

    skill_rules = {
        (rule.rule, tuple(sorted(rule.methods - {"HEAD", "OPTIONS"})))
        for rule in flask_app.url_map.iter_rules()
        if rule.rule.startswith("/apps/<uuid:app_id>/skills")
    }
    assert skill_rules == {("/apps/<uuid:app_id>/skills", ("GET",))}
