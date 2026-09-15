"""工作台模型选项的公开响应，不包含运行输入或身份上下文。"""

from pydantic import BaseModel, ConfigDict, Field
from internal.entity.agent_execution_entity import AgentModelSelection


class AgentProtocolModelSettingsResp(BaseModel):
    """会话设置仅返回生效模型与切换权限，账号目录由语言模型接口提供。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    model: str = ""
    provider: str = ""
    selected_model: AgentModelSelection | None = None
    model_override: AgentModelSelection | None = None
    can_select_model: bool = False
    reasoning_levels: list[str] = Field(default_factory=list)
    default_reasoning_effort: str | None = None
    selected_reasoning_effort: str | None = None
