"""Agent 运行时装配服务测试。"""

from pathlib import Path
import re
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from config.config import Config
from internal.schema.agent_runtime_schema import AgentRuntimePrepareReq
from internal.service.agent_runtime_service import (
    AgentRuntimeService,
    build_filesystem_permissions,
)


def _runtime_service(engine=object()) -> AgentRuntimeService:
    """构造不经过 Injector 的测试服务。"""
    service = AgentRuntimeService.__new__(AgentRuntimeService)
    service.db = SimpleNamespace(engine=engine)
    service.skill_service = MagicMock()
    return service


def _prepare_req(**updates) -> AgentRuntimePrepareReq:
    """构造最小运行时装配请求。"""
    values = {
        "account_id": uuid4(),
        "app_id": uuid4(),
        "conversation_id": uuid4(),
        "created_by": uuid4(),
        "end_user_id": None,
        "skill_bindings": ({"name": "research"},),
        "subagents": (),
    }
    values.update(updates)
    return AgentRuntimePrepareReq(**values)


def test_prepare_uses_req_and_keeps_successful_session_owned_by_backend(monkeypatch):
    """装配成功后 Session 由 Workspace Backend 生命周期托管。"""
    import internal.service.agent_runtime_service as runtime_module

    runtime_session = MagicMock()
    settings = SimpleNamespace()
    resources = object()
    service = _runtime_service()
    service.skill_service.list_enabled_latest.return_value = ["skill"]
    service._assemble_resources = MagicMock(return_value=resources)
    monkeypatch.setattr(runtime_module, "Config", lambda: settings)
    monkeypatch.setattr(
        runtime_module,
        "sessionmaker",
        lambda **_kwargs: lambda: runtime_session,
    )
    req = _prepare_req()

    result = service.prepare(req)

    assert result is resources
    service.skill_service.list_enabled_latest.assert_called_once_with(
        account_id=req.account_id,
        app_id=req.app_id,
        bindings=[{"name": "research"}],
    )
    service._assemble_resources.assert_called_once_with(
        runtime_session=runtime_session,
        req=req,
        skills=["skill"],
        settings=settings,
    )
    runtime_session.close.assert_not_called()


def test_prepare_closes_runtime_session_when_assembly_fails(monkeypatch):
    """运行时装配失败时关闭独立数据库 Session。"""
    import internal.service.agent_runtime_service as runtime_module

    runtime_session = MagicMock()
    service = _runtime_service()
    service.skill_service.list_enabled_latest.return_value = []
    service._assemble_resources = MagicMock(side_effect=RuntimeError("装配失败"))
    monkeypatch.setattr(runtime_module, "Config", lambda: SimpleNamespace())
    monkeypatch.setattr(
        runtime_module,
        "sessionmaker",
        lambda **_kwargs: lambda: runtime_session,
    )

    with pytest.raises(RuntimeError, match="装配失败"):
        service.prepare(_prepare_req(skill_bindings=()))

    runtime_session.close.assert_called_once_with()


def test_prepare_validates_config_before_opening_database_session(monkeypatch):
    """配置错误不得占用运行时数据库连接。"""
    import internal.service.agent_runtime_service as runtime_module

    service = _runtime_service()
    service.skill_service.list_enabled_latest.return_value = []
    sessionmaker_mock = MagicMock()
    monkeypatch.setattr(
        runtime_module,
        "Config",
        MagicMock(side_effect=ValueError("配置错误")),
    )
    monkeypatch.setattr(runtime_module, "sessionmaker", sessionmaker_mock)

    with pytest.raises(ValueError, match="配置错误"):
        service.prepare(_prepare_req(skill_bindings=()))

    sessionmaker_mock.assert_not_called()


def test_assemble_resources_projects_req_into_runtime_context(monkeypatch):
    """请求身份和 SubAgent 配置应原样进入核心装配器。"""
    import internal.service.agent_runtime_service as runtime_module

    assembler = MagicMock()
    assembler.assemble.return_value = object()
    monkeypatch.setattr(
        runtime_module,
        "AgentContextAssembler",
        MagicMock(return_value=assembler),
    )
    service = _runtime_service()
    runtime_session = MagicMock()
    settings = SimpleNamespace()
    req = _prepare_req(subagents=({"name": "researcher"},))

    result = service._assemble_resources(
        runtime_session=runtime_session,
        req=req,
        skills=["skill"],
        settings=settings,
    )

    assert result is assembler.assemble.return_value
    call = assembler.assemble.call_args.kwargs
    assert call["runtime_session"] is runtime_session
    assert call["run_context"].account_id == req.account_id
    assert call["run_context"].conversation_id == req.conversation_id
    assert call["subagents"] == [{"name": "researcher"}]
    assert call["settings"] is settings


def test_internal_code_has_no_legacy_agent_imports():
    """可执行代码只通过统一的 Agent V2 路径导入。"""
    root = Path(__file__).resolve().parents[3] / "internal"
    legacy_import = re.compile(
        r"(?:from\s+internal\.core\.agent\s+import|"
        r"import\s+internal\.core\.agent(?:\s|$))"
    )

    matches = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if legacy_import.search(path.read_text(encoding="utf-8"))
    ]

    assert matches == []


def test_filesystem_permissions_end_with_global_deny():
    """未知路径和私有命名空间保持默认拒绝。"""
    permissions = build_filesystem_permissions()

    assert permissions[-1].mode == "deny"
    assert permissions[-1].paths == ["/**"]
    assert set(permissions[-1].operations) == {"read", "write"}
    assert "/workspace/.audit/**" in permissions[0].paths
    assert "/workspace/.system/**" in permissions[0].paths


@pytest.mark.parametrize(
    ("key", "value"),
    [("WORKSPACE_MAX_BYTES", "0"), ("WORKSPACE_MAX_FILES", "-1")],
)
def test_invalid_workspace_security_config_fails_fast(monkeypatch, key, value):
    """Workspace 限额不能被关闭。"""
    monkeypatch.setenv(key, value)

    with pytest.raises(ValueError, match="Workspace limits"):
        Config()


def test_production_requires_explicit_workspace_limits(monkeypatch):
    """生产环境不能继承开发限额默认值。"""
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.delenv("WORKSPACE_MAX_BYTES", raising=False)
    monkeypatch.delenv("WORKSPACE_MAX_FILES", raising=False)

    with pytest.raises(ValueError, match="required in production"):
        Config()


def test_invalid_sandbox_provider_fails_fast(monkeypatch):
    """未知 Sandbox provider 不能进入运行时配置。"""
    monkeypatch.setenv("FLASK_ENV", "production")
    monkeypatch.setenv("WORKSPACE_MAX_BYTES", "1024")
    monkeypatch.setenv("WORKSPACE_MAX_FILES", "10")
    monkeypatch.setenv("AGENT_SANDBOX_PROVIDER", "bubblewrap")

    with pytest.raises(ValueError, match="AGENT_SANDBOX_PROVIDER"):
        Config()


def test_invalid_memory_backend_fails_fast(monkeypatch):
    """未知长期记忆 Store 不能进入运行时配置。"""
    monkeypatch.setenv("AGENT_MEMORY_BACKEND", "sqlite")

    with pytest.raises(ValueError, match="AGENT_MEMORY_BACKEND"):
        Config()
