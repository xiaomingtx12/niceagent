"""Agent Workspace 配额领域规则测试。"""

from dataclasses import dataclass

import pytest

from internal.core.agent_v2.entity.workspace_quota import (
    AgentWorkspaceLimits,
    WorkspaceQuotaExceeded,
    WorkspaceQuotaKind,
)


@dataclass(frozen=True)
class _FileUsage:
    """配额测试使用的最小文件视图。"""

    path: str
    size_bytes: int
    is_deleted: bool = False


def test_overwrite_uses_replaced_file_size() -> None:
    """覆盖文件时只计算替换后的大小，不重复增加文件数。"""
    limits = AgentWorkspaceLimits(max_bytes=10, max_files=1)

    limits.check_write([_FileUsage("a.txt", 8)], path="a.txt", new_size=10)


def test_file_count_overflow_reports_typed_quota_kind() -> None:
    """文件数超限通过稳定枚举标识具体配额维度。"""
    limits = AgentWorkspaceLimits(max_bytes=100, max_files=1)

    with pytest.raises(WorkspaceQuotaExceeded) as error:
        limits.check_write([_FileUsage("a.txt", 1)], path="b.txt", new_size=1)

    assert error.value.kind is WorkspaceQuotaKind.FILES
    assert error.value.maximum == 1


def test_byte_overflow_reports_typed_quota_kind() -> None:
    """总容量超限与文件数超限使用不同的稳定枚举值。"""
    limits = AgentWorkspaceLimits(max_bytes=10, max_files=2)

    with pytest.raises(WorkspaceQuotaExceeded) as error:
        limits.check_write([_FileUsage("a.txt", 8)], path="b.txt", new_size=3)

    assert error.value.kind is WorkspaceQuotaKind.BYTES
    assert error.value.maximum == 10


def test_deleted_files_do_not_consume_quota() -> None:
    """已删除记录不占用当前 Workspace 文件数和容量。"""
    limits = AgentWorkspaceLimits(max_bytes=10, max_files=1)

    limits.check_write(
        [_FileUsage("old.txt", 100, is_deleted=True)],
        path="new.txt",
        new_size=10,
    )


def test_negative_file_size_is_rejected() -> None:
    """负数大小不能绕过总容量校验。"""
    limits = AgentWorkspaceLimits(max_bytes=10, max_files=1)

    with pytest.raises(ValueError, match="不能小于 0"):
        limits.check_write([], path="a.txt", new_size=-1)
