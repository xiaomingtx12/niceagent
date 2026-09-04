"""Agent Workspace 稳定作用域测试。"""

from uuid import UUID

from internal.core.agent_v2.entity.workspace_scope import build_workspace_scope_id


def test_workspace_scope_id_is_a_persisted_compatibility_contract() -> None:
    """固定输入始终生成相同 v1 标识，避免既有文件失联。"""
    scope_id = build_workspace_scope_id(
        UUID("11111111-1111-1111-1111-111111111111"),
        UUID("22222222-2222-2222-2222-222222222222"),
    )

    assert scope_id == UUID("e27537cb-5b51-58d7-a472-3c5e2352c821")
