"""内置 Skill 文件系统适配器回归测试。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from internal.repository.builtin_skill_repository import (
    BuiltinSkillRepository,
    BuiltinSkillRepositoryError,
)


def test_relative_root_is_resolved_from_project_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """启动目录不影响项目内置 Skill 的加载。"""
    monkeypatch.chdir(tmp_path)
    repository = BuiltinSkillRepository(
        SimpleNamespace(BUILTIN_SKILLS_ROOT="resources/agent_skills/builtin")
    )

    catalog = repository.load_catalog()

    assert "agent-browser" in catalog


def test_missing_root_raises_repository_error(tmp_path: Path) -> None:
    """配置目录缺失时明确报告平台资源故障。"""
    repository = BuiltinSkillRepository(
        SimpleNamespace(BUILTIN_SKILLS_ROOT=str(tmp_path / "missing"))
    )

    with pytest.raises(BuiltinSkillRepositoryError, match="不存在"):
        repository.load_catalog()
