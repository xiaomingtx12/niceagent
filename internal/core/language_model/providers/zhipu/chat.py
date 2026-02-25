from langchain_openai import ChatOpenAI

from internal.core.language_model.entities.model_entity import BaseLanguageModel


class Chat(ChatOpenAI, BaseLanguageModel):
    """智谱AI聊天模型（基于OpenAI兼容接口）"""

    openai_api_base: str = "https://open.bigmodel.cn/api/paas/v4"
