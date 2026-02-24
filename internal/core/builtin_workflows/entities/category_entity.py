from pydantic import BaseModel, Field


class WorkflowCategoryEntity(BaseModel):
    """工作流模板分类实体类"""

    category: str = Field(default="")
    name: str = Field(default="")
