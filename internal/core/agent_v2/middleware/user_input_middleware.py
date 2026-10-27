"""主图用户提问能力的装配入口。"""

from langchain.agents.middleware.types import AgentMiddleware

from internal.core.agent_v2.tools.ask_user import ask_user


class UserInputMiddleware(AgentMiddleware):
    """仅向主图注入提问工具，避免默认通用子 Agent 继承该能力。"""

    tools = [ask_user]
