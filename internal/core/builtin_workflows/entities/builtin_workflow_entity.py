from typing import Any

from pydantic import BaseModel, Field


class BuiltinWorkflowEntity(BaseModel):
    """内置工作流模板实体类"""

    id: str = Field(default="")
    category: str = Field(default="")
    name: str = Field(default="")
    tool_call_name: str = Field(default="")  # 工作流工具调用名称
    icon: str = Field(default="")
    description: str = Field(default="")
    nodes: list[dict[str, Any]] = Field(default_factory=list)  # 节点配置
    edges: list[dict[str, Any]] = Field(default_factory=list)  # 边配置
    created_at: int = Field(default=0)
