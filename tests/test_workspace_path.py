"""Agent Workspace 路径领域规则测试。"""

from uuid import UUID

import pytest

from internal.core.agent_v2.entity.workspace_path import (
    AgentWorkspacePathPolicy,
    WorkspacePathError,
)


_SCOPE_ID = UUID("11111111-1111-1111-1111-111111111111")


def test_workspace_root_is_only_a_query_prefix() -> None:
    """根目录可以用于列表，但不能作为文件读写。"""
    paths = AgentWorkspacePathPolicy(scope_id=_SCOPE_ID)

    assert paths.resolve_prefix("/workspace") == str(_SCOPE_ID)
    assert paths.virtual_path(str(_SCOPE_ID)) == "/workspace"
    with pytest.raises(WorkspacePathError, match="root is a directory"):
        paths.resolve_file("/workspace")


def test_storage_path_never_exceeds_persistence_limit() -> None:
    """追加 scope 后的物理路径仍不得超过数据库字段长度。"""
    paths = AgentWorkspacePathPolicy(scope_id=_SCOPE_ID)
    maximum_relative = "a" * 475

    assert len(paths.resolve_file(f"/workspace/{maximum_relative}")) == 512
    with pytest.raises(WorkspacePathError, match="storage limit"):
        paths.resolve_file(f"/workspace/{maximum_relative}a")


def test_uuid_named_directories_remain_inside_current_scope() -> None:
    """UUID 目录名不靠形状猜测拦截，隔离由可信 scope 前缀保证。"""
    paths = AgentWorkspacePathPolicy(scope_id=_SCOPE_ID)
    relative = (
        "22222222-2222-2222-2222-222222222222/"
        "33333333-3333-3333-3333-333333333333/file.txt"
    )

    storage_path = paths.resolve_file(f"/workspace/{relative}")

    assert storage_path == f"{_SCOPE_ID}/{relative}"
    assert paths.contains(storage_path)
    assert paths.virtual_path(storage_path) == f"/workspace/{relative}"


@pytest.mark.parametrize(
    "path",
    [
        "/workspace/../secret.txt",
        "/workspace/a//b.txt",
        "/workspace/a\\b.txt",
    ],
)
def test_illegal_path_segments_are_rejected(path: str) -> None:
    """目录穿越、空片段和非 POSIX 分隔符均被拒绝。"""
    paths = AgentWorkspacePathPolicy(scope_id=_SCOPE_ID)

    with pytest.raises(WorkspacePathError):
        paths.resolve_file(path)


def test_contains_rejects_corrupted_storage_path() -> None:
    """数据库中的异常路径不能仅凭 scope 字符串前缀通过检查。"""
    paths = AgentWorkspacePathPolicy(scope_id=_SCOPE_ID)

    assert not paths.contains(f"{_SCOPE_ID}/../secret.txt")
