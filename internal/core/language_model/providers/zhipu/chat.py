from langchain_community.chat_models import ChatZhipuAI

from internal.core.language_model.entities.model_entity import BaseLanguageModel


class Chat(ChatZhipuAI, BaseLanguageModel):
    """智谱AI聊天模型"""

    pass
