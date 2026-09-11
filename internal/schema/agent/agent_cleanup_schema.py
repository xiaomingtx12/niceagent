"""Agent 派生资源清理请求，不包含运行输入或页面响应。"""

from uuid import UUID
from pydantic import BaseModel, ConfigDict


class AgentConversationCleanupReq(BaseModel):
    """清理会话派生资源的请求。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: UUID
    app_id: UUID
    conversation_id: UUID



class AgentMessageCleanupReq(AgentConversationCleanupReq):
    """清理消息所在会话可恢复状态的请求。"""

    message_id: UUID



class AgentAppCleanupReq(BaseModel):
    """清理应用下全部 Agent 派生资源的请求。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_id: UUID
    app_id: UUID
