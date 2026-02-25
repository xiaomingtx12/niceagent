from langchain_openai import ChatOpenAI

from internal.core.language_model.entities.model_entity import BaseLanguageModel


class Chat(ChatOpenAI, BaseLanguageModel):
    """通义千问聊天模型（基于OpenAI兼容接口）"""

    openai_api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
