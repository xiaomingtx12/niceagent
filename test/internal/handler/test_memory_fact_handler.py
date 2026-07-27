"""MemoryFactHandler 单测.

覆盖 6 个端点 (WebApp 4 + creator 2):
- GET  /web-apps/<token>/memory/facts  → list_my_memory_facts
- POST /web-apps/<token>/memory/facts  → create_my_memory_fact
- GET  /web-apps/<token>/memory/facts/<fact_id> → get_my_memory_fact
- DELETE /web-apps/<token>/memory/facts/<fact_id> → delete_my_memory_fact
- GET  /apps/<app_id>/memory/facts     → list_app_memory_facts
- DELETE /apps/<app_id>/memory/facts/<fact_id> → delete_app_memory_fact

约束 (硬约束 #5): handler 只做参数解析 + 鉴权 + 序列化; 业务逻辑由
MemoryFactService 保证. handler 单测只验证 handler 正确调用 service +
返回正确结构, 不重复测业务逻辑 (service 已在 test_memory_fact_service
覆盖). 用 unittest.mock 隔离 service + Flask test client 模拟 request
context (current_user).

注: WebApp 端点鉴权 master 现状是用 login_required + Account; 真正 anonymous
end-user 鉴权待 R5+. 本测试按 master 模式覆盖.
"""
from datetime import datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from flask import Flask
from flask_login import LoginManager


# ---------- 通用 fixture ----------

@pytest.fixture
def account():
    a = MagicMock()
    a.id = uuid4()
    a.is_authenticated = True  # flask_login 检查
    a.is_active = True
    return a


@pytest.fixture
def app():
    a = MagicMock()
    a.id = uuid4()
    a.account_id = uuid4()
    return a


@pytest.fixture
def flask_app(account):
    """Flask app + LoginManager + user_loader + 已登录 context."""
    from flask_login import login_user

    app = Flask(__name__)
    app.config["SECRET_KEY"] = "test"
    lm = LoginManager()
    lm.init_app(app)

    @lm.user_loader
    def _load_user(user_id):
        return account  # 测试恒返回 mock account

    with app.test_request_context():
        # 主动 login_user 让 current_user 解析到 account
        login_user(account)
        yield app, account


@pytest.fixture
def handler(flask_app, app):
    """构造 handler + 把 mock service 装上. flask_app 已激活 request context."""
    _flask, account = flask_app
    from internal.handler.memory_fact_handler import MemoryFactHandler

    h = MemoryFactHandler(
        memory_fact_service=MagicMock(),
        web_app_service=MagicMock(),
        app_service=MagicMock(),
    )
    h.web_app_service.get_web_app.return_value = app
    h.app_service.get_app.return_value = app
    return h, app, account


def _fact(**overrides):
    f = MagicMock()
    f.id = overrides.get("id", uuid4())
    f.to_dict.return_value = overrides.get("to_dict", {"id": str(f.id), "content": "x"})
    return f


def _body(resp):
    """pkg.response.success_json 返回 (jsonify_response, status) tuple."""
    return resp[0].get_json()


# ---------- end-user 端点 (WebApp token) ----------


def test_list_my_memory_facts_calls_service(handler):
    h, app, account = handler
    fact = _fact(to_dict={"id": "f1", "content": "hello"})
    h.memory_fact_service.list_end_user_facts.return_value = [fact]

    resp = h.list_my_memory_facts(token="abc")

    h.web_app_service.get_web_app.assert_called_once_with("abc")
    h.memory_fact_service.list_end_user_facts.assert_called_once_with(
        account_id=str(app.account_id),
        app_id=str(app.id),
        end_user_id=str(account.id),
    )
    body = _body(resp)
    # Response envelope: {code, message, data: {items, total}}
    assert body["data"]["total"] == 1
    assert body["data"]["items"] == [{"id": "f1", "content": "hello"}]


def test_create_my_memory_fact_calls_service(handler):
    h, app, account = handler
    fact = _fact(to_dict={"id": "new", "content": "manual"})
    h.memory_fact_service.create_fact.return_value = fact

    # 模拟 wtforms validate 通过
    req_instance = MagicMock()
    req_instance.validate.return_value = True
    req_instance.fact_type.data = "preference"
    req_instance.content.data = "manual"
    req_instance.confidence.data = 0.95
    with patch(
        "internal.handler.memory_fact_handler.CreateMemoryFactReq",
        return_value=req_instance,
    ):
        resp = h.create_my_memory_fact(token="abc")

    h.memory_fact_service.create_fact.assert_called_once()
    kwargs = h.memory_fact_service.create_fact.call_args.kwargs
    assert kwargs["fact_type"] == "preference"
    assert kwargs["content"] == "manual"
    assert kwargs["confidence"] == 0.95
    assert kwargs["auto_extracted"] is False
    assert kwargs["end_user_id"] == str(account.id)
    assert _body(resp)["data"] == {"id": "new", "content": "manual"}


def test_get_my_memory_fact_found(handler):
    h, app, account = handler
    target_id = uuid4()
    fact = _fact(id=target_id, to_dict={"id": str(target_id), "content": "found"})
    other = _fact(to_dict={"id": str(uuid4()), "content": "other"})
    h.memory_fact_service.list_end_user_facts.return_value = [other, fact]

    resp = h.get_my_memory_fact(token="abc", fact_id=target_id)

    assert _body(resp)["data"] == {"id": str(target_id), "content": "found"}


def test_get_my_memory_fact_not_found(handler):
    h, app, account = handler
    h.memory_fact_service.list_end_user_facts.return_value = []

    resp = h.get_my_memory_fact(token="abc", fact_id=uuid4())

    # 找不到 -> 返回 errors dict
    assert _body(resp)["data"] == {"fact_id": "fact 不存在或无权访问"}


def test_delete_my_memory_fact_calls_service_actor_end_user(handler):
    h, app, account = handler
    fact_id = uuid4()

    resp = h.delete_my_memory_fact(token="abc", fact_id=fact_id)

    h.memory_fact_service.soft_delete_fact.assert_called_once_with(
        account_id=str(app.account_id),
        fact_id=fact_id,
        actor_end_user_id=str(account.id),
        actor_is_creator=False,
    )
    assert "删除成功" in (_body(resp).get("message") or "")


# ---------- creator 端点 ----------


def test_list_app_memory_facts_calls_service(handler):
    h, app, account = handler
    fact = _fact(to_dict={"id": "c1", "content": "creator view"})
    h.memory_fact_service.list_creator_facts.return_value = [fact]

    req_instance = MagicMock()
    req_instance.validate.return_value = True
    req_instance.end_user_id.data = "eu1"
    req_instance.fact_type.data = "instruction"
    with patch(
        "internal.handler.memory_fact_handler.GetMemoryFactsReq",
        return_value=req_instance,
    ):
        resp = h.list_app_memory_facts(app_id=app.id)

    h.app_service.get_app.assert_called_once_with(app.id, account)
    h.memory_fact_service.list_creator_facts.assert_called_once_with(
        account_id=str(app.account_id),
        app_id=str(app.id),
        end_user_id_filter="eu1",
        fact_type_filter="instruction",
    )
    assert _body(resp)["data"]["total"] == 1


def test_delete_app_memory_fact_actor_is_creator(handler):
    h, app, account = handler
    fact_id = uuid4()

    resp = h.delete_app_memory_fact(app_id=app.id, fact_id=fact_id)

    h.app_service.get_app.assert_called_once_with(app.id, account)
    h.memory_fact_service.soft_delete_fact.assert_called_once_with(
        account_id=str(app.account_id),
        fact_id=fact_id,
        actor_is_creator=True,
    )
    assert "删除成功" in (_body(resp).get("message") or "")