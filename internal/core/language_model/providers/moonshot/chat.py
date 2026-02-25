from langchain_openai import ChatOpenAI

from internal.core.language_model.entities.model_entity import BaseLanguageModel


class Chat(ChatOpenAI, BaseLanguageModel):
    """月之暗面聊天模型（基于OpenAI兼容接口）"""

    openai_api_base: str = "https://api.moonshot.cn/v1"
