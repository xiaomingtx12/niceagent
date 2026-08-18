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


def test_scan_reads_skill_package_files(tmp_path):
    root = tmp_path / "builtin"
    skill = root / "csv-analysis"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "run.py").write_text("print('ok')", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        """---
name: csv-analysis
description: Analyze CSV files
metadata:
  my_dify:
    version: 1.0.0
    scripts:
      - id: run
        path: scripts/run.py
        runtime: python3
        timeout_seconds: 10
        max_output_bytes: 1024
        input_count: 0
        output_kind: json
---
""",
        encoding="utf-8",
    )
    instance = SkillService.__new__(SkillService)
    instance._root = root

    row = instance.list_catalog()[0]

    assert "scripts/run.py" in row.package_files
    assert row.package_root == skill.resolve()
