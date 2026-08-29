"""Agent Workspace 路径和容量规则异常。"""


class WorkspacePathError(ValueError):
    """虚拟路径越过允许作用域时抛出。"""


class WorkspaceQuotaExceeded(ValueError):
    """写入超过 Workspace 容量时抛出。"""

    def __init__(self, limit: str, maximum: int) -> None:
        """记录超限维度且不包含文件内容。"""
        self.limit = limit
        self.maximum = maximum
        super().__init__(f"Workspace {limit} quota exceeded ({maximum})")


class WorkspaceStorageError(RuntimeError):
    """Workspace 内容存储不可用或返回无效数据时抛出。"""


__all__ = [
    "WorkspacePathError",
    "WorkspaceQuotaExceeded",
    "WorkspaceStorageError",
]
