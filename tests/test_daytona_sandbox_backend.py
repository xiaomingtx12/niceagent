"""Daytona Sandbox Provider 异常转换回归测试。"""

from types import SimpleNamespace

import pytest
from daytona.common.errors import DaytonaError, DaytonaRateLimitError

import internal.core.agent_v2.backends.sandbox.daytona_sandbox_backend as daytona_backend
from internal.core.agent_v2.backends.sandbox.daytona_sandbox_backend import (
    DaytonaResourceQuotaExceeded,
    ManagedDaytonaSandbox,
    _daytona_resource_dimension,
    _is_daytona_quota_error,
)


def test_daytona_cpu_quota_error_is_typed_and_actionable() -> None:
    """CPU 配额错误应明确告知免费套餐的三项资源上限。"""
    error = DaytonaError("resource limit exceeded: cpu capacity")

    assert _is_daytona_quota_error(error, str(error))
    assert _daytona_resource_dimension(error, str(error)) == "CPU"
    quota_error = DaytonaResourceQuotaExceeded(
        resource="CPU",
        detail=str(error),
    )
    assert "CPU 已超出可用配额" in str(quota_error)
    assert "内存 10 GiB" in str(quota_error)
    assert "存储 30 GiB" in str(quota_error)


def test_daytona_non_quota_error_is_not_classified_as_quota() -> None:
    """认证失败等普通 Provider 错误不能被误报为资源配额不足。"""
    error = DaytonaError("invalid api key", status_code=401)

    assert not _is_daytona_quota_error(error, str(error))


def test_daytona_rate_limit_is_not_classified_as_resource_quota() -> None:
    """429 请求频率限制应保留为限流错误，而不是资源容量错误。"""
    error = DaytonaRateLimitError("rate limit exceeded", status_code=429)

    assert not _is_daytona_quota_error(error, str(error))


def test_create_backend_surfaces_daytona_quota_error(monkeypatch) -> None:
    """创建阶段的 Daytona 配额错误应穿透为专用异常。"""
    closed = False

    class _Workspace:
        def close(self) -> None:
            nonlocal closed
            closed = True

    class _Client:
        def create(self):
            raise DaytonaError("disk quota exceeded", code="RESOURCE_QUOTA_EXCEEDED")

    monkeypatch.setattr(daytona_backend, "Daytona", lambda _config: _Client())

    with pytest.raises(DaytonaResourceQuotaExceeded, match="disk quota exceeded"):
        daytona_backend.create_backend(
            settings=SimpleNamespace(
                DAYTONA_API_KEY="test",
                DAYTONA_API_URL="https://example.invalid",
                AGENT_SANDBOX_SNAPSHOT_ID=None,
                AGENT_SANDBOX_EXECUTE_TIMEOUT_SECONDS=30,
            ),
            workspace=_Workspace(),
            sandbox_name="test",
            skill_files=[],
        )

    assert closed


def test_upload_files_surfaces_daytona_storage_quota_error() -> None:
    """运行期间上传文件触发存储配额时也应抛出专用异常。"""
    class _Filesystem:
        def upload_files(self, _files):
            raise DaytonaError("storage quota exceeded")

    backend = ManagedDaytonaSandbox(
        sandbox=SimpleNamespace(id="sandbox", fs=_Filesystem()),
        workspace=SimpleNamespace(),
    )

    with pytest.raises(DaytonaResourceQuotaExceeded) as error:
        backend.upload_files([("/workspace/result.txt", b"content")])

    assert error.value.resource == "存储"
    assert "30 GiB" in str(error.value)
