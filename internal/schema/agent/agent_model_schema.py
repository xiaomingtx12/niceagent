"""工作台模型选项的公开响应，不包含运行输入或身份上下文。"""

from pydantic import BaseModel, ConfigDict, Field
from internal.entity.agent_run_entity import AgentModelSelection


class WorkbenchModelChoiceResp(BaseModel):
    """工作台模型目录仅公开标识、名称和推理等级。"""

    provider: str
    model: str
    provider_connection_id: str
    label: str
    connection_name: str
    reasoning_levels: list[str] = Field(default_factory=list)
    default_reasoning_effort: str | None = None



class WorkbenchModelSettingsResp(BaseModel):
    """只有创建者入口公开可切换的模型目录。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    model: str = ""
    provider: str = ""
    selected_model: AgentModelSelection | None = None
    model_override: AgentModelSelection | None = None
    available_models: list[WorkbenchModelChoiceResp] = Field(default_factory=list)
    reasoning_levels: list[str] = Field(default_factory=list)
    default_reasoning_effort: str | None = None
    selected_reasoning_effort: str | None = None
