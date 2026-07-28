from typing import Any
from uuid import UUID

from langchain_core.tools import BaseTool
from langgraph.graph import MessagesState
from pydantic import BaseModel, ConfigDict, Field, field_validator

from internal.core.agent_v2.context import BASE_AGENT_PROMPT
from internal.entity.app_entity import DEFAULT_APP_CONFIG
from internal.entity.conversation_entity import InvokeFrom


# Backward-compatible exports. Both agent modes now share DeepAgents' native
# graph and one layered prompt assembly path.
AGENT_SYSTEM_PROMPT_TEMPLATE = BASE_AGENT_PROMPT
REACT_AGENT_SYSTEM_PROMPT_TEMPLATE = BASE_AGENT_PROMPT


class AgentConfig(BaseModel):
    """All business inputs needed to compile one DeepAgents graph."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    user_id: UUID
    invoke_from: InvokeFrom = InvokeFrom.WEB_APP

    app_name: str = "智能助手"
    app_description: str = "提供准确、可靠的任务协助。"
    preset_prompt: str = ""
    opening_statement: str = ""

    tools: list[BaseTool] = Field(default_factory=list)
    review_config: dict = Field(
        default_factory=lambda: DEFAULT_APP_CONFIG["review_config"]
    )
    max_iteration_count: int = 10

    account_id: UUID | None = None
    app_id: UUID | None = None
    conversation_id: UUID | None = None
    end_user_id: UUID | None = None
    workflow_variables: dict[str, Any] = Field(default_factory=dict)
    # Final platform schema is list-shaped; legacy dict input is normalized by
    # validate_platform_subagents at the API boundary.
    subagents: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("subagents", mode="before")
    @classmethod
    def normalize_legacy_subagents(cls, value):
        if isinstance(value, dict):
            return [
                {"name": name, **config}
                for name, config in value.items()
                if isinstance(config, dict)
            ]
        return value


class AgentState(MessagesState):
    task_id: UUID
    iteration_count: int = 0


DATASET_RETRIEVAL_TOOL_NAME = "dataset_retrieval"
MAX_ITERATION_RESPONSE = "当前Agent迭代次数已超过限制，请重试"
