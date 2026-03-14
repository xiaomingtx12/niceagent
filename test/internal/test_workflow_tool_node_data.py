from uuid import uuid4

from internal.core.workflow.entities.node_entity import NodeType
from internal.core.workflow.nodes.tool.tool_entity import ToolNodeData


def test_tool_node_data_supports_mcp_tool_type():
    node_data = ToolNodeData(
        id=uuid4(),
        node_type=NodeType.TOOL,
        title="mcp-tool-node",
        type="mcp_tool",
        provider_id=str(uuid4()),
        tool_id="echo_text",
        params={},
        inputs=[],
        outputs=[],
    )

    assert node_data.tool_type == "mcp_tool"
