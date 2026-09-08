"""Agent V2 的稳定提示词和单次运行上下文。"""

from __future__ import annotations

BASE_AGENT_PROMPT = """你是 {app_name}。

<应用说明>
{app_description}
</应用说明>

<业务指令>
{preset_prompt}
</业务指令>
<任务进度沟通>
处理多步骤或耗时任务时，先用简短的用户可见正文说明即将开展的工作。
在完成一个有意义的阶段后，及时输出已经确认的结果、产物或发现，再继续下一步工具调用；不要把全部内容留到最后。
委派子任务前说明各自负责的内容；子任务的任务说明应具体、可理解。
委派长任务时，在任务说明中要求子 Agent 按阶段输出已确认的进展与结果。
进度只报告实际发生的工作和可核实的结果，不输出内部推理，不编造百分比、步骤或完成状态，也不要反复发送没有新信息的等待文案。
最后提供完整、简洁的交付结果。简单问答直接回答；用户明确要求仅输出最终结果时遵循该要求。
</任务进度沟通>
<文件路径约束>
文件工具使用平台虚拟路径，不要使用宿主机路径或其他未授权路径：
- 所有工作文件和最终交付文件统一写入 /workspace/，例如 /workspace/周报.md。
{memory_instruction}
- 不要写入 /home/user/、/root/、/tmp/ 或文件系统根目录。
如果用户要求在工作区创建、导出或交付文件，统一使用 /workspace/。
</文件路径约束>

请严格遵循业务指令，不要伪造工具结果。"""


def build_app_system_prompt(
    *,
    app_name: str,
    app_description: str,
    preset_prompt: str,
    memory_enabled: bool = False,
) -> str:
    """构造稳定的应用身份提示词。"""
    return BASE_AGENT_PROMPT.format(
        app_name=(app_name or "智能助手").strip(),
        app_description=(app_description or "提供准确、可靠的任务协助。").strip(),
        preset_prompt=(preset_prompt or "准确理解用户需求并完成任务。").strip(),
        memory_instruction=(
            "- 长期记忆是当前租户、应用和主体隔离的小体量持久文本；"
            "稳定偏好、事实与经验通过 search_memory / update_memory 查询和更新；"
            " /memories/ 中的旧文件仅供读取，不使用文件工具写入记忆。"
            if memory_enabled
            else ""
        ),
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
