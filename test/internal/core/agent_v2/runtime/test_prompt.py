"""Agent 系统提示词的路径约束测试。"""

from internal.core.agent_v2.runtime.prompt import build_app_system_prompt


def test_system_prompt_exposes_virtual_file_paths():
    """模型应明确使用平台 Backend 暴露的虚拟路径。"""
    prompt = build_app_system_prompt(
        app_name="周报助手",
        app_description="生成周报",
        preset_prompt="完成用户要求",
    )

    assert "/workspace/" in prompt
    assert "/memories/" in prompt
    assert "/workspace/artifacts/" not in prompt
    assert "\n- 最终交付文件写入 /artifacts/" not in prompt
    assert "/memories/" in prompt
    assert "/home/user/" in prompt
    assert "/tmp/" in prompt
