"""Agent V2 Backend 适配器的延迟导出。"""

from importlib import import_module

_EXPORTS = {
    "build_composite_backend": (
        "internal.core.agent_v2.backends.composite",
        "build_composite_backend",
    ),
    "ReadOnlyCatalogBackend": (
        "internal.core.agent_v2.backends.read_only",
        "ReadOnlyCatalogBackend",
    ),
    "SkillCatalogBackend": (
        "internal.core.agent_v2.backends.skill_catalog",
        "SkillCatalogBackend",
    ),
    "ManagedLangSmithSandbox": (
        "internal.core.agent_v2.backends.sandbox",
        "ManagedLangSmithSandbox",
    ),
    "SandboxProviderError": (
        "internal.core.agent_v2.backends.sandbox",
        "SandboxProviderError",
    ),
    "build_stable_sandbox_name": (
        "internal.core.agent_v2.backends.sandbox",
        "build_stable_sandbox_name",
    ),
    "build_sandbox_backend": (
        "internal.core.agent_v2.backends.sandbox",
        "build_sandbox_backend",
    ),
    "MyDifyWorkspaceBackend": (
        "internal.core.agent_v2.backends.workspace",
        "MyDifyWorkspaceBackend",
    ),
    "WorkspaceRouteBackend": (
        "internal.core.agent_v2.backends.workspace",
        "WorkspaceRouteBackend",
    ),
}


def __getattr__(name: str):
    """按需加载 Backend，避免协议适配器互相初始化。"""
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    return getattr(import_module(module_name), attribute)


__all__ = list(_EXPORTS)
