"""Agent V2 的业务输入、轨迹和结果模型。"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from uuid import UUID

from langchain_core.tools import BaseTool
from pydantic import BaseModel, ConfigDict, Field

from internal.entity.app_entity import DEFAULT_APP_CONFIG
from internal.entity.conversation_entity import InvokeFrom, MessageStatus


class AgentConfig(BaseModel):
    """编译 DeepAgents 图所需的业务输入。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    user_id: UUID
    invoke_from: InvokeFrom = InvokeFrom.WEB_APP

    app_name: str = "智能助手"
    app_description: str = "提供准确、可靠的任务协助。"
    preset_prompt: str = ""
    # 应用首屏展示文案，不参与 Agent system prompt。
    opening_statement: str = ""

    tools: list[BaseTool] = Field(default_factory=list)
    review_config: dict = Field(
        default_factory=lambda: DEFAULT_APP_CONFIG["review_config"]
    )
    max_model_calls: int = Field(default=10, ge=1)
    max_tool_calls: int = Field(default=40, ge=1)

    # 平台配置保存边界已经统一为标准化列表。
    subagents: list[dict[str, Any]] = Field(default_factory=list)


DATASET_RETRIEVAL_TOOL_NAME = "dataset_retrieval"


class QueueEvent(str, Enum):
    """队列事件类型枚举。"""

    AGENT_THOUGHT = "agent_thought"
    AGENT_MESSAGE = "agent_message"
    AGENT_ACTION = "agent_action"
    TODO_UPDATE = "todo_update"
    SUB_AGENT_DELEGATE = "sub_agent_delegate"
    SUB_AGENT_END = "sub_agent_end"
    DATASET_RETRIEVAL = "dataset_retrieval"
    AGENT_END = "agent_end"
    TIMEOUT = "timeout"
    PING = "ping"
    STOP = "stop"
    ERROR = "error"


class AgentType(str, Enum):
    """轨迹事件所属的 Agent 角色。"""

    MAIN = "main"
    SUBAGENT = "subagent"


class AgentTodo(BaseModel):
    """DeepAgents 原生 Todo 状态项。"""

    content: str
    status: Literal["pending", "in_progress", "completed"]


class AgentThought(BaseModel):
    """智能体推理观察输出内容。"""

    id: UUID
    task_id: UUID
    event: QueueEvent
    thought: str = ""
    observation: str = ""

    tool: str = ""
    tool_input: dict = Field(default_factory=dict)

    agent_id: str = "main"
    agent_type: AgentType = AgentType.MAIN
    parent_agent_id: str | None = None
    delegation_id: str | None = None
    background_task_id: str | None = None
    todos: list[AgentTodo] = Field(default_factory=list)

    message: list[dict] = Field(default_factory=list)
    message_token_count: int = 0
    message_unit_price: float = 0
    message_price_unit: float = 0

    answer: str = ""
    answer_token_count: int = 0
    answer_unit_price: float = 0
    answer_price_unit: float = 0

    total_token_count: int = 0
    total_price: float = 0
    latency: float = 0


class AgentResult(BaseModel):
    """智能体推理观察最终结果。"""

    query: str = ""
    image_urls: list[str] = Field(default_factory=list)

    message: list[dict] = Field(default_factory=list)
    message_token_count: int = 0
    message_unit_price: float = 0
    message_price_unit: float = 0

    answer: str = ""
    answer_token_count: int = 0
    answer_unit_price: float = 0
    answer_price_unit: float = 0

    total_token_count: int = 0
    total_price: float = 0
    latency: float = 0

    status: str = MessageStatus.NORMAL
    error: str = ""

    agent_thoughts: list[AgentThought] = Field(default_factory=list)


__all__ = [
    "AgentConfig",
    "AgentResult",
    "AgentThought",
    "AgentTodo",
    "AgentType",
    "DATASET_RETRIEVAL_TOOL_NAME",
    "QueueEvent",
]
