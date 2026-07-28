"""Layered prompt assembly for DeepAgents."""

from __future__ import annotations

from typing import Any


BASE_AGENT_PROMPT = """你是 {app_name}。

<应用说明>
{app_description}
</应用说明>

<业务指令>
{preset_prompt}
</业务指令>

请严格遵循业务指令，按需使用已授权工具和 Skills。不要伪造工具结果；需要委派时选择最合适的子 Agent。"""


def build_app_system_prompt(
    *,
    app_name: str,
    app_description: str,
    preset_prompt: str,
    opening_statement: str = "",
) -> str:
    """Build the stable L1 application identity block."""
    prompt = BASE_AGENT_PROMPT.format(
        app_name=(app_name or "智能助手").strip(),
        app_description=(app_description or "提供准确、可靠的任务协助。").strip(),
        preset_prompt=(preset_prompt or "准确理解用户需求并完成任务。").strip(),
    )
    if opening_statement.strip():
        prompt += (
            "\n\n<首轮开场约束>\n"
            f"{opening_statement.strip()}\n"
            "</首轮开场约束>"
        )
    return prompt


def build_dynamic_context(
    *,
    invoked_at: str,
    account_id: str,
    end_user_id: str | None,
    workflow_variables: dict[str, Any] | None,
) -> str:
    """Build the per-run L3 block appended after stable prompt prefixes."""
    lines = [
        "<运行时上下文>",
        f"当前时间: {invoked_at}",
        f"账号标识: {account_id}",
    ]
    if end_user_id:
        lines.append(f"终端用户标识: {end_user_id}")
    if workflow_variables:
        lines.append("工作流变量:")
        for key in sorted(workflow_variables):
            lines.append(f"- {key}: {workflow_variables[key]}")
    lines.append("</运行时上下文>")
    return "\n".join(lines)


__all__ = [
    "BASE_AGENT_PROMPT",
    "build_app_system_prompt",
    "build_dynamic_context",
]
