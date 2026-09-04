"""Agent 平台工具的结构化输入。"""

from pydantic import BaseModel, Field


class CreateAppToolReq(BaseModel):
    """自动创建 Agent 应用的工具输入。"""

    name: str = Field(description="需要创建的Agent/应用名称，长度不超过50个字符")
    description: str = Field(
        description="需要创建的Agent/应用描述，请详细概括该应用的功能"
    )


class WorkflowGeneratorToolReq(BaseModel):
    """自动生成工作流的工具输入。"""

    name: str = Field(description="工作流名称，简洁明了，不超过50个字符")
    description: str = Field(description="工作流描述，概括工作流的功能")
    requirements: str = Field(
        description="详细的工作流需求说明，包括需要的功能、处理流程、节点类型等"
    )
    node_types: str = Field(
        default="",
        description=(
            "可选，指定需要使用的节点类型，多个类型用逗号分隔，如："
            "start,llm,template_transform,end。留空则自动选择"
        ),
    )


class AppWithWorkflowGeneratorToolReq(BaseModel):
    """自动创建 Agent 应用及配套工作流的工具输入。"""

    app_name: str = Field(description="Agent应用名称，简洁明了，不超过50个字符")
    app_description: str = Field(description="Agent应用描述，概括应用的功能")
    preset_prompt: str = Field(
        description="Agent的预设提示词，定义Agent的人设、角色和回复逻辑"
    )
    workflow_requirements: str = Field(
        description="需要生成的工作流的详细需求说明，包括功能、处理流程等"
    )
    auto_publish: bool = Field(
        default=False,
        description="是否自动发布应用和工作流，默认为False（保存为草稿）",
    )


class DatasetRetrievalToolReq(BaseModel):
    """知识库检索工具输入。"""

    query: str = Field(description="知识库搜索query语句，类型为字符串")


__all__ = [
    "AppWithWorkflowGeneratorToolReq",
    "CreateAppToolReq",
    "DatasetRetrievalToolReq",
    "WorkflowGeneratorToolReq",
]
