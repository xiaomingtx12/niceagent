"""Builtin Skill catalog and binding tests."""

from pathlib import Path

import pytest

from internal.exception import FailException
from internal.service.skill_service import SkillService


@pytest.fixture
def service(tmp_path):
    root = tmp_path / "builtin"
    _write_skill(root / "research", "research", "Research carefully")
    _write_skill(root / "writer", "writer", "Write clearly")
    instance = SkillService.__new__(SkillService)
    instance._root = root
    return instance


def _write_skill(path: Path, name: str, description: str):
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        (
            "---\n"
            f"name: {name}\n"
            f"description: {description}\n"
            "metadata:\n"
            "  my_dify:\n"
            "    version: 1.0.0\n"
            "---\n\n"
            f"# {name}\n"
        ),
        encoding="utf-8",
    )


def test_list_catalog_reads_builtin_skill_metadata(service):
    rows = service.list_catalog()

    assert [row.name for row in rows] == ["research", "writer"]
    assert rows[0].version == "1.0.0"
    assert rows[0].digest.startswith("sha256:")
    assert "# research" in rows[0].skill_md


def test_normalize_bindings_validates_version_digest_and_duplicates(service):
    research = service._scan()["research"]

    binding = service.normalize_bindings(
        [{"name": "research", "version": research.version, "digest": research.digest}]
    )

    assert binding == [
        {
            "name": "research",
            "version": research.version,
            "digest": research.digest,
            "enabled": True,
        }
    ]
    with pytest.raises(FailException) as duplicate:
        service.normalize_bindings([{"name": "research"}, {"name": "research"}])
    assert "不能重复" in duplicate.value.message
    with pytest.raises(FailException) as mismatched:
        service.normalize_bindings([{"name": "research", "digest": "sha256:bad"}])
    assert "版本或摘要不匹配" in mismatched.value.message


def test_list_enabled_latest_returns_only_bound_enabled_skills(service):
    rows = service.list_enabled_latest(
        account_id="unused",
        app_id="unused",
        bindings=[
            {"name": "research"},
            {"name": "writer", "enabled": False},
        ],
    )

    assert [row.name for row in rows] == ["research"]
