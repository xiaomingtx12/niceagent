"""内置模板与插件升级的契约和执行回归，不连接数据库或付费服务。"""

from contextlib import nullcontext
from dataclasses import fields
from importlib import import_module
import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
import requests

from internal.core.builtin_apps import BuiltinAppManager
from internal.core.builtin_apps.entities.builtin_app_entity import BuiltinAppEntity
from internal.core.tools.builtin_tools.providers import BuiltinProviderManager
from internal.model import AppConfigVersion
from internal.repository.builtin_skill_repository import BuiltinSkillRepository
from internal.schema.app_config_schema import DraftAppConfigPatchReq
from internal.schema.builtin_app_schema import GetBuiltinAppsResp
from internal.schema.builtin_tool_schema import BuiltinProviderResp
from internal.service.app_config_service import AppConfigService
from internal.service.builtin_app_service import BuiltinAppService
from internal.service.builtin_tool_service import BuiltinToolService
from internal.service.skill_service import SkillService


@pytest.fixture(scope="module")
def providers():
    """真实加载目录，确保工厂、元数据和运行名称保持一致。"""
    return BuiltinProviderManager()


@pytest.fixture(scope="module")
def config_service(providers):
    """使用真实 Skill 目录与业务校验，只替换数据库和模型依赖。"""
    service = AppConfigService(
        **{field.name: Mock() for field in fields(AppConfigService)}
    )
    service.builtin_provider_manager = providers
    service.db.session.query.return_value.filter.return_value.all.return_value = []
    service.skill_service = SkillService(
        BuiltinSkillRepository(
            SimpleNamespace(BUILTIN_SKILLS_ROOT="resources/agent_skills/builtin")
        )
    )
    return service


def test_all_templates_resolve_current_skills_and_authorized_subagents(
    config_service, providers
):
    """每个模板必须经过真实草稿校验，子 Agent 引用只来自主 Agent。"""
    apps = BuiltinAppManager().get_builtin_apps()
    assert len(apps) == 14
    assert len({app.id for app in apps}) == len(apps)
    for app in apps:
        config = config_service.validate_draft_app_config(
            DraftAppConfigPatchReq.from_mapping(app.to_draft_config()), uuid4()
        )
        assert config["skills"]
        assert all(skill["version"] and skill["digest"] for skill in config["skills"])
        skill_names = {skill["name"] for skill in config["skills"]}
        tool_names = {tool["tool_id"] for tool in config["tools"]}
        for agent in config["subagents"]:
            assert set(agent["skills"]) <= skill_names
            assert set(agent["tools"]) <= tool_names
            assert agent["mode"] == "sync"


def test_template_copy_preserves_capabilities_and_uses_account_model(config_service):
    """创建真实 ORM 草稿对象，防止绑定字段在服务层丢失。"""
    manager = BuiltinAppManager()
    template = next(
        app for app in manager.get_builtin_apps() if app.name == "专题研究团队"
    )
    db = Mock()
    db.auto_commit.side_effect = lambda: nullcontext()
    persisted = []
    db.session.add.side_effect = persisted.append
    db.session.flush.side_effect = lambda: setattr(persisted[-1], "id", uuid4())
    model_service = Mock()
    account = SimpleNamespace(id=uuid4())
    model_service.get_default_model_config.return_value = {
        "provider": "account-provider",
        "model": "active-model",
        "parameters": {},
    }
    service = BuiltinAppService(db, manager, model_service, config_service)
    app = service.add_builtin_app_to_space(template.id, account)
    draft = next(
        call.args[0]
        for call in db.session.add.call_args_list
        if isinstance(call.args[0], AppConfigVersion)
    )
    assert app.account_id == account.id
    assert draft.app_id == app.id
    assert draft.id is not None
    assert draft.model_config["model"] == "active-model"
    assert {skill["name"] for skill in draft.skills} == {
        skill["name"] for skill in template.skills
    }
    assert len(draft.subagents) == 2
    assert draft.tools == template.tools
    assert draft.user_input == {"enable": True}
    assert draft.workflows == []
    assert app.draft_app_config_id == draft.id
    model_service.get_default_model_config.assert_called_once_with(account.id)


def test_template_defaults_are_isolated_and_preview_has_no_fixed_model():
    """修改一次模板或草稿不会污染其他模板及平台默认值。"""
    first, second = BuiltinAppEntity(), BuiltinAppEntity()
    first.review_config["inputs_config"]["enable"] = True
    assert second.review_config["inputs_config"]["enable"] is False
    app = BuiltinAppManager().get_builtin_apps()[0]
    preview = GetBuiltinAppsResp().dump(app)
    assert preview["model_config"] == {}
    assert preview["skills"] and preview["requirements"]
    config = app.to_draft_config()
    config["skills"].clear()
    assert app.skills
    assert "tags" not in config and "requirements" not in config


def test_catalog_instantiation_needs_no_credentials_and_names_match(
    providers, monkeypatch
):
    """工具运行名称必须匹配绑定、Skill 授权及子 Agent 工具引用。"""
    for provider in providers.get_providers():
        for env in provider.provider_entity.required_env:
            monkeypatch.delenv(env, raising=False)
        for definition in provider.get_tool_entities():
            factory = provider.get_tool(definition.name)
            tool = factory(**{param.name: param.default for param in definition.params})
            assert tool.name == definition.name
            assert tool.get_input_schema().model_json_schema()["properties"]
            assert BuiltinToolService.get_tool_inputs(factory)


def test_configuration_status_does_not_expose_secrets(providers, monkeypatch):
    """状态只根据配置存在性生成，国内可用字段不充当运行就绪状态。"""
    service = BuiltinToolService(providers, Mock())
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    tools = service.get_builtin_tools()
    google = next(tool for tool in tools if tool["name"] == "google")
    assert google["configuration_status"] == "needs_configuration"
    assert google["missing_config"] == ["SERPER_API_KEY"]
    monkeypatch.setenv("SERPER_API_KEY", "private-test-token")
    serialized = BuiltinProviderResp(many=True).dump(service.get_builtin_tools())
    assert "private-test-token" not in json.dumps(serialized)
    assert (
        next(tool for tool in serialized if tool["name"] == "google")[
            "configuration_status"
        ]
        == "configured"
    )
    assert (
        next(tool for tool in serialized if tool["name"] == "time")[
            "configuration_status"
        ]
        == "no_configuration"
    )


@pytest.mark.parametrize(
    "provider,name,env",
    [
        ("searx", "searx_search", "SEARX_SEARCH_URL"),
        ("google", "google_serper", "SERPER_API_KEY"),
        ("dalle", "dalle3", "OPENAI_API_KEY"),
        ("tongyi", "qwen_image_generation", "DASHSCOPE_API_KEY"),
        ("gaode", "gaode_weather", "GAODE_API_KEY"),
    ],
)
def test_missing_configuration_fails_only_when_invoked(
    providers, monkeypatch, provider, name, env
):
    """缺少外部服务配置不能阻止应用启动。"""
    monkeypatch.delenv(env, raising=False)
    tool = providers.get_tool(provider, name)()
    assert env in tool.invoke({"city" if provider == "gaode" else "query": "test"})


def test_searx_preserves_sources_and_honors_limit(providers, monkeypatch):
    """低关键词重合的搜索结果仍保留可验证的原始链接。"""
    module = import_module(
        "internal.core.tools.builtin_tools.providers.searx.searx_search"
    )
    monkeypatch.setenv("SEARX_SEARCH_URL", "https://search.example")
    get = Mock(
        return_value=Mock(
            json=lambda: {
                "results": [
                    {
                        "title": "资料",
                        "url": f"https://source.example/{i}",
                        "content": "独立研究证据",
                    }
                    for i in range(5)
                ]
            }
        )
    )
    monkeypatch.setattr(module.requests, "get", get)
    result = json.loads(
        providers.get_tool("searx", "searx_search")(max_results=3).invoke(
            {"query": "复杂问题"}
        )
    )
    assert len(result["results"]) == 3
    assert result["results"][0]["url"] == "https://source.example/0"
    assert get.call_args.kwargs["timeout"] == 20
    get.side_effect = requests.Timeout("private deployment details")
    assert "超时" in providers.get_tool("searx", "searx_search")().invoke(
        {"query": "test"}
    )


def test_weather_retains_explicit_key_and_handles_missing_city(providers, monkeypatch):
    """工厂不能丢失传入凭据；空行政区响应不能继续查询天气。"""
    module = import_module(
        "internal.core.tools.builtin_tools.providers.gaode.gaode_weather"
    )
    session = Mock()
    session.get.return_value = Mock(json=lambda: {"status": "1", "districts": []})
    monkeypatch.setattr(module.requests, "Session", lambda: nullcontext(session))
    result = providers.get_tool("gaode", "gaode_weather")(
        api_key="explicit-key"
    ).invoke({"city": "不存在"})
    assert "未找到城市" in result
    assert session.get.call_args.kwargs["params"]["key"] == "explicit-key"
    assert session.get.call_count == 1


def test_image_parameters_and_environment_key_reach_provider(providers, monkeypatch):
    """通义图像不再出现文档支持环境变量但实际忽略的情况。"""
    module = import_module(
        "internal.core.tools.builtin_tools.providers.tongyi.qwen_image_generation"
    )
    monkeypatch.setenv("DASHSCOPE_API_KEY", "image-key")
    call = Mock(
        return_value=SimpleNamespace(
            status_code=200,
            output=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=[{"image": "https://images.example/result.png"}]
                        )
                    )
                ]
            ),
        )
    )
    monkeypatch.setattr(module.MultiModalConversation, "call", call)
    tool = providers.get_tool("tongyi", "qwen_image_generation")(size="1328*1328")
    assert tool.invoke({"query": "test"}) == "https://images.example/result.png"
    assert call.call_args.kwargs["api_key"] == "image-key"
    assert call.call_args.kwargs["size"] == "1328*1328"


def test_time_is_timezone_aware_and_rejects_unknown_zone(providers):
    """容器在 UTC 运行也不会影响显式业务时区。"""
    tool = providers.get_tool("time", "current_time")()
    assert "+08:00" in tool.invoke({})
    assert "+00:00" in tool.invoke({"timezone": "UTC"})
    assert "不支持的时区" in tool.invoke({"timezone": "Missing/Zone"})


def test_duckduckgo_result_limit_reaches_sdk(providers, monkeypatch):
    """SDK 使用 num_results 别名，max_results 会被静默忽略。"""
    tool = providers.get_tool("duckduckgo", "duckduckgo_search")(max_results=3)
    results = Mock(
        return_value=[
            {"title": "资料", "link": "https://source.example", "snippet": "内容"}
        ]
    )
    monkeypatch.setattr(type(tool.api_wrapper), "results", results)
    assert (
        json.loads(tool.invoke({"query": "test"}))[0]["link"]
        == "https://source.example"
    )
    results.assert_called_once_with("test", 3, source="text")


def test_dalle_style_and_size_reach_sdk(providers, monkeypatch):
    """旧包装器忽略 style，新实现必须把用户配置传给图片 API。"""
    module = import_module("internal.core.tools.builtin_tools.providers.dalle.dalle3")
    client = Mock()
    client.images.generate.return_value = SimpleNamespace(
        data=[SimpleNamespace(url="https://images.example/result.png")]
    )
    monkeypatch.setattr(module, "OpenAI", lambda **kwargs: nullcontext(client))
    result = providers.get_tool("dalle", "dalle3")(
        api_key="explicit-key", style="natural", size="1792x1024"
    ).invoke({"query": "test"})
    assert result == "https://images.example/result.png"
    assert client.images.generate.call_args.kwargs["style"] == "natural"
    assert client.images.generate.call_args.kwargs["size"] == "1792x1024"


def test_google_sources_and_provider_failure(providers, monkeypatch):
    """保留来源链接，并隐藏可能包含凭据的请求异常。"""
    module = import_module(
        "internal.core.tools.builtin_tools.providers.google.google_serper"
    )
    post = Mock(
        return_value=Mock(
            json=lambda: {
                "organic": [
                    {
                        "title": "资料",
                        "link": "https://source.example",
                        "snippet": "内容",
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(module.requests, "post", post)
    tool = providers.get_tool("google", "google_serper")(
        serper_api_key="explicit-key", max_results=3
    )
    assert (
        json.loads(tool.invoke({"query": "test"}))["results"][0]["url"]
        == "https://source.example"
    )
    assert post.call_args.kwargs["json"]["num"] == 3
    post.side_effect = requests.HTTPError("secret=explicit-key")
    result = tool.invoke({"query": "test"})
    assert "失败" in result and "explicit-key" not in result


def test_wikipedia_returns_ranked_articles_and_links(providers, monkeypatch):
    """MediaWiki 按搜索排序返回结果，并保留规范原文链接。"""
    module = import_module(
        "internal.core.tools.builtin_tools.providers.wikipedia.wikipedia_search"
    )
    get = Mock(
        return_value=Mock(
            json=lambda: {
                "query": {
                    "pages": {
                        "2": {
                            "index": 2,
                            "title": "第二项",
                            "fullurl": "https://en.wikipedia.org/2",
                            "extract": "two",
                        },
                        "1": {
                            "index": 1,
                            "title": "第一项",
                            "fullurl": "https://en.wikipedia.org/1",
                            "extract": "one",
                        },
                    }
                }
            }
        )
    )
    monkeypatch.setattr(module.requests, "get", get)
    result = json.loads(
        providers.get_tool("wikipedia", "wikipedia_search")(
            lang="en", max_results=1
        ).invoke({"query": "test"})
    )
    assert result["results"] == [
        {"title": "第一项", "url": "https://en.wikipedia.org/1", "snippet": "one"}
    ]
    assert get.call_args.args[0] == "https://en.wikipedia.org/w/api.php"
