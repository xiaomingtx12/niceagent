"""Agent 调用链路专用业务异常。"""

from __future__ import annotations

from .exception import FailException


class AgentAttachmentException(FailException):
    """附件读取或 Workspace 投影失败。"""


class AgentInvocationException(FailException):
    """Agent 调用上下文或运行配置不满足业务约束。"""


class SubAgentConfigError(ValueError):
    """SubAgent 配置不安全或无效。"""


__all__ = [
    "AgentAttachmentException",
    "AgentInvocationException",
    "SubAgentConfigError",
]
