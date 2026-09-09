"""商业工具只模拟平台响应，不写入真实客户或项目数据。"""

import json
from contextlib import nullcontext
from unittest.mock import Mock

import pytest
import requests

from internal.core.tools.builtin_tools import business_http
from internal.core.tools.builtin_tools.providers.feishu import tools as feishu
from internal.core.tools.builtin_tools.providers.notion import tools as notion
from internal.core.tools.builtin_tools.providers import BuiltinProviderManager
from internal.service.builtin_tool_service import BuiltinToolService


FEISHU_CONFIG = dict(
    app_id="cli_example",
    app_secret="test-secret",
    app_token="bascnExample",
    table_id="tblExample",
)


def test_business_catalog_separates_credentials_from_model_inputs():
    """目录与运行工具可无凭据加载，模型输入不能覆盖目标表或密钥。"""
    manager = BuiltinProviderManager()
    for provider_id in ("feishu", "notion"):
        provider = manager.get_provider(provider_id)
        assert (
            BuiltinToolService.get_provider_configuration(provider.provider_entity)[
                "configuration_status"
            ]
            == "configure_in_app"
        )
        for definition in provider.get_tool_entities():
            factory = provider.get_tool(definition.name)
            config = {
                param.name: "test-secret" if param.secret else "test-id"
                for param in definition.params
            }
            tool = factory(**config)
            schema = tool.get_input_schema().model_json_schema()
            assert not set(config).intersection(schema["properties"])
            assert "test-secret" not in repr(tool)
            assert "test-secret" not in json.dumps(tool.model_dump(), default=str)
            assert "test-secret" not in str(tool.to_json())


def test_feishu_search_passes_filter_and_cursor(monkeypatch):
    """确保筛选和分页是真实 API 参数，返回记录可供后续更新使用。"""
    api = Mock(
        side_effect=[
            {"tenant_access_token": "tenant-test"},
            {
                "data": {
                    "items": [{"record_id": "rec1"}],
                    "has_more": True,
                    "page_token": "next",
                }
            },
        ]
    )
    monkeypatch.setattr(feishu, "api_request", api)
    filters = {
        "conjunction": "and",
        "conditions": [{"field_name": "状态", "operator": "is", "value": ["跟进中"]}],
    }
    result = json.loads(
        feishu.feishu_record_search(**FEISHU_CONFIG).invoke(
            {
                "filter_json": json.dumps(filters),
                "page_token": "cursor",
                "page_size": 10,
            }
        )
    )
    request = api.call_args
    assert request.args == (
        "feishu",
        "POST",
        "bitable/v1/apps/bascnExample/tables/tblExample/records/search",
    )
    assert request.kwargs["payload"] == {"filter": filters}
    assert request.kwargs["params"] == {"page_size": 10, "page_token": "cursor"}
    assert result["data"]["page_token"] == "next"


@pytest.mark.parametrize(
    "factory,inputs,method,suffix",
    [
        (
            feishu.feishu_record_create,
            {"fields_json": '{"客户":"公司"}'},
            "POST",
            "/records",
        ),
        (
            feishu.feishu_record_update,
            {"fields_json": '{"状态":"已跟进"}', "record_id": "rec1"},
            "PUT",
            "/records/rec1",
        ),
    ],
)
def test_feishu_writes_only_configured_table(
    monkeypatch, factory, inputs, method, suffix
):
    """每次只提交一条记录，更新不会附带未明确修改的字段。"""
    api = Mock(
        side_effect=[
            {"tenant_access_token": "tenant-test"},
            {"data": {"record": {"record_id": "rec1"}}},
        ]
    )
    monkeypatch.setattr(feishu, "api_request", api)
    result = json.loads(factory(**FEISHU_CONFIG).invoke(inputs))
    assert result["data"]["record"]["record_id"] == "rec1"
    assert api.call_count == 2
    assert api.call_args.args[1] == method
    assert api.call_args.args[2].endswith("tables/tblExample" + suffix)
    assert api.call_args.kwargs["payload"] == {
        "fields": json.loads(inputs["fields_json"])
    }


@pytest.mark.parametrize(
    "inputs",
    [
        {"fields_json": "[]", "record_id": "rec1"},
        {"fields_json": "{}", "record_id": "rec1"},
        {"fields_json": '{"a":1}', "record_id": "../other"},
    ],
)
def test_invalid_feishu_write_is_rejected_before_auth(monkeypatch, inputs):
    """坏数据与路径注入不应触发任何网络请求。"""
    api = Mock()
    monkeypatch.setattr(feishu, "api_request", api)
    assert "error" in json.loads(
        feishu.feishu_record_update(**FEISHU_CONFIG).invoke(inputs)
    )
    api.assert_not_called()


def test_notion_creates_bounded_paragraphs_under_configured_parent(monkeypatch):
    """长文本拆分遵守 Notion 限额，返回真实创建页面的链接。"""
    api = Mock(return_value={"id": "new-page", "url": "https://notion.so/new-page"})
    monkeypatch.setattr(notion, "api_request", api)
    tool = notion.notion_page_create(
        integration_token="test-secret", parent_page_id="parent-id"
    )
    result = json.loads(
        tool.invoke({"title": "客户纪要", "content": "一" * 2100 + "\n跟进计划"})
    )
    payload = api.call_args.kwargs["payload"]
    assert payload["parent"] == {"page_id": "parent-id"}
    assert len(payload["children"]) == 3
    assert (
        len(payload["children"][0]["paragraph"]["rich_text"][0]["text"]["content"])
        == 2000
    )
    assert result["data"]["url"] == "https://notion.so/new-page"


def test_notion_append_and_read_retain_child_ids(monkeypatch):
    """追加仅指向配置页面；嵌套块和游标供模型继续读取。"""
    api = Mock(
        return_value={
            "results": [
                {
                    "id": "child",
                    "type": "toggle",
                    "has_children": True,
                    "toggle": {"rich_text": []},
                }
            ],
            "has_more": True,
            "next_cursor": "next",
        }
    )
    monkeypatch.setattr(notion, "api_request", api)
    result = json.loads(
        notion.notion_page_read(integration_token="secret").invoke(
            {"block_id": "page-id", "start_cursor": "cursor"}
        )
    )
    assert api.call_args.kwargs["params"]["start_cursor"] == "cursor"
    assert result["data"]["blocks"][0]["has_children"] is True
    assert result["data"]["next_cursor"] == "next"
    notion.notion_page_append(
        integration_token="secret", parent_page_id="fixed-parent"
    ).invoke({"content": "跟进记录"})
    assert api.call_args.args == ("notion", "PATCH", "blocks/fixed-parent/children")


def test_notion_search_returns_titles_and_urls(monkeypatch):
    """剔除无关页面元数据，保留供阅读调用的真实页面 ID。"""
    api = Mock(
        return_value={
            "results": [
                {
                    "id": "page",
                    "url": "https://notion.so/page",
                    "properties": {
                        "Name": {"type": "title", "title": [{"plain_text": "项目"}]}
                    },
                }
            ],
            "has_more": False,
        }
    )
    monkeypatch.setattr(notion, "api_request", api)
    result = json.loads(
        notion.notion_page_search(integration_token="secret").invoke({"query": "项目"})
    )
    assert result["data"]["pages"][0]["title"] == "项目"
    assert api.call_args.kwargs["payload"]["filter"]["value"] == "page"


def test_http_write_timeout_is_not_retried_or_exposed(monkeypatch):
    """创建请求超时可能已落库，既不能自动重发，也不能泄露请求异常。"""
    session = Mock()
    session.request.side_effect = requests.Timeout("Authorization: secret")
    monkeypatch.setattr(business_http.requests, "Session", lambda: nullcontext(session))
    with pytest.raises(business_http.BusinessToolError) as error:
        business_http.api_request("notion", "POST", "pages", token="secret", payload={})
    assert "未知" in str(error.value) and "secret" not in str(error.value)
    session.request.assert_called_once()
    assert session.request.call_args.kwargs["allow_redirects"] is False
    assert session.trust_env is False


@pytest.mark.parametrize(
    "status,body",
    [
        (403, b'{"message":"secret"}'),
        (200, b'{"code":999,"msg":"secret"}'),
        (200, b"not-json"),
    ],
)
def test_http_provider_errors_are_sanitized(monkeypatch, status, body):
    """HTTP 与业务错误不能把平台回显的密钥或请求载荷交给模型。"""
    response = Mock(status_code=status)
    response.iter_content.return_value = [body]
    session = Mock()
    session.request.return_value = nullcontext(response)
    monkeypatch.setattr(business_http.requests, "Session", lambda: nullcontext(session))
    with pytest.raises(business_http.BusinessToolError) as error:
        business_http.api_request("feishu", "POST", "path", token="secret")
    assert "secret" not in str(error.value)
