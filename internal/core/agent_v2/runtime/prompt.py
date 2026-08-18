"""Agent V2 的稳定提示词和单次运行上下文。"""

from __future__ import annotations

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
) -> str:
    """构造稳定的应用身份提示词。"""
    return BASE_AGENT_PROMPT.format(
        app_name=(app_name or "智能助手").strip(),
        app_description=(app_description or "提供准确、可靠的任务协助。").strip(),
        preset_prompt=(preset_prompt or "准确理解用户需求并完成任务。").strip(),
    )


def build_dynamic_context(*, invoked_at: str) -> str:
    """构造单次模型调用追加的运行时间上下文。"""
    return "\n".join(
        [
            "<运行时上下文>",
            f"当前时间: {invoked_at}",
            "</运行时上下文>",
        ]
    )


__all__ = [
    "BASE_AGENT_PROMPT",
    "build_app_system_prompt",
    "build_dynamic_context",
]
