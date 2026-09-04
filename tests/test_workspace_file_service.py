"""Workspace 文件业务服务回归测试。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from internal.core.agent_v2.entity.workspace_scope import build_workspace_scope_id
from internal.exception import ValidateErrorException
from internal.service.workspace_file_service import WorkspaceFileService


class _UnexpectedFileStore:
    """确保非法路径不会进入持久化实现。"""

    def write_text(self, *_args, **_kwargs):
        """任何写入都代表路径策略被绕过。"""
        raise AssertionError("非法路径不应写入存储")


class _RecordingFileStore:
    """记录业务服务交给持久化实现的物理路径。"""

    def __init__(self) -> None:
        """初始化尚未写入的记录。"""
        self.path: str | None = None

    def write_text(self, _repository, path: str, _content: str, **_kwargs):
        """记录路径并返回轻量文件结果。"""
        self.path = path
        return SimpleNamespace(path=path)


def test_write_rejects_workspace_root_as_file() -> None:
    """HTTP 写入不能把 Workspace 根目录当成文件。"""
    service = WorkspaceFileService(
        db=SimpleNamespace(session=object()),
        config=SimpleNamespace(WORKSPACE_MAX_BYTES=1024, WORKSPACE_MAX_FILES=10),
        file_store=_UnexpectedFileStore(),
    )

    with pytest.raises(ValidateErrorException, match="Workspace 路径不合法"):
        service.write_text(
            account_id=uuid4(),
            app_id=uuid4(),
            path="/workspace",
            content="secret",
            created_by=uuid4(),
        )


def test_write_uses_stable_workspace_scope_path() -> None:
    """业务服务只通过统一路径策略生成物理存储路径。"""
    account_id = uuid4()
    app_id = uuid4()
    file_store = _RecordingFileStore()
    service = WorkspaceFileService(
        db=SimpleNamespace(session=object()),
        config=SimpleNamespace(WORKSPACE_MAX_BYTES=1024, WORKSPACE_MAX_FILES=10),
        file_store=file_store,
    )

    service.write_text(
        account_id=account_id,
        app_id=app_id,
        path="/workspace/notes/today.md",
        content="hello",
        created_by=account_id,
    )

    assert file_store.path == (
        f"{build_workspace_scope_id(account_id, app_id)}/notes/today.md"
    )
