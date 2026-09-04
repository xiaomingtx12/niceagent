"""Workspace 文件一致性存储回归测试。"""

from types import SimpleNamespace

import pytest

from internal.repository.workspace_file_repository import WorkspaceStorageError
from internal.repository.workspace_file_store import WorkspaceFileStore


class _UnexpectedObjectStorage:
    """确保已删除文件不会继续访问对象存储。"""

    def get_bytes(self, *_args, **_kwargs):
        """任何内容读取都代表删除状态判断失效。"""
        raise AssertionError("不应读取已删除文件")

    def get_download_url(self, *_args, **_kwargs):
        """任何地址生成都代表删除状态判断失效。"""
        raise AssertionError("不应为已删除文件生成下载地址")


def test_read_deleted_file_raises_workspace_storage_error() -> None:
    """已删除文件使用 Workspace 统一存储异常。"""
    store = WorkspaceFileStore(object_storage=_UnexpectedObjectStorage())
    row = SimpleNamespace(is_deleted=True, object_key="sensitive/object-key")

    with pytest.raises(WorkspaceStorageError, match="已删除"):
        store.read_bytes(row)


def test_download_deleted_file_raises_workspace_storage_error() -> None:
    """已删除文件下载与读取保持相同的异常接口。"""
    store = WorkspaceFileStore(object_storage=_UnexpectedObjectStorage())
    row = SimpleNamespace(is_deleted=True, object_key="sensitive/object-key")

    with pytest.raises(WorkspaceStorageError, match="已删除"):
        store.get_download_url(row, expires_seconds=60)
