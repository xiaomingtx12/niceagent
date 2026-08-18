"""Long-term memory profile backend tests."""

from types import SimpleNamespace

from internal.core.agent_v2.memory.projection import (
    MemoryProfileBackend,
    MemoryProfileRenderer,
)


def test_memory_profile_renderer_outputs_stable_markdown():
    content = MemoryProfileRenderer().render(
        [
            SimpleNamespace(fact_type="preference", content="  用户偏好中文  "),
            SimpleNamespace(fact_type="project", content="项目使用 PostgreSQL 16"),
            SimpleNamespace(fact_type="empty", content=" "),
        ]
    )

    assert content == (
        "# Long-term memory\n\n"
        "- [preference] 用户偏好中文\n"
        "- [project] 项目使用 PostgreSQL 16\n"
    )


def test_memory_profile_backend_is_read_only():
    backend = MemoryProfileBackend("# Long-term memory\n\n- fact\n")

    read = backend.read("/profile.md")
    assert read.error is None
    assert "- fact" in read.file_data["content"]
    assert backend.write("/profile.md", "changed").error is not None
    assert backend.edit("/profile.md", "fact", "other").error is not None


def test_memory_profile_backend_lists_official_and_legacy_paths():
    backend = MemoryProfileBackend("content")

    paths = {entry["path"] for entry in backend.ls("/").entries}
    assert paths == {"/AGENTS.md", "/profile.md"}
    assert backend.read("/missing.md").error is not None
