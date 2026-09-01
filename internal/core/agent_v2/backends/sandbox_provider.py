"""通用 Sandbox Provider 异常。"""

from __future__ import annotations


class SandboxProviderError(RuntimeError):
    """Sandbox provider 创建、文件同步或清理失败。"""


__all__ = ["SandboxProviderError"]
