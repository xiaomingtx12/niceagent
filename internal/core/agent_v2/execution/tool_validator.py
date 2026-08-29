"""Agent 工具集合的运行时校验。"""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.tools import BaseTool

from internal.exception import FailException


DEEPAGENTS_RESERVED_TOOL_NAMES = frozenset(
    {
        "task",
        "write_todos",
        "ls",
        "read_file",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "execute",
    }
)


def validate_agent_tools(tools: Sequence[BaseTool]) -> list[BaseTool]:
    """复制工具列表，并拒绝重名或覆盖框架内置工具。"""
    resolved = list(tools)
    names = [tool.name for tool in resolved]
    if len(names) != len(set(names)):
        raise FailException("Agent 工具名称冲突，无法安全装配运行时工具")

    reserved = sorted(set(names) & DEEPAGENTS_RESERVED_TOOL_NAMES)
    if reserved:
        raise FailException(
            "Agent 工具名称与 DeepAgents 内置工具冲突: " + ", ".join(reserved)
        )
    return resolved


__all__ = ["DEEPAGENTS_RESERVED_TOOL_NAMES", "validate_agent_tools"]
