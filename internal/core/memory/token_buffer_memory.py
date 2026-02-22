from dataclasses import dataclass

from langchain_core.language_models import BaseLanguageModel
from langchain_core.messages import (
    AnyMessage,
    HumanMessage,
    AIMessage,
    trim_messages,
    get_buffer_string,
)
from sqlalchemy import desc
import tiktoken

from internal.entity.conversation_entity import MessageStatus
from internal.model import Conversation, Message
from pkg.sqlalchemy import SQLAlchemy


@dataclass
class TokenBufferMemory:
    """基于token计数的缓冲记忆组件"""

    db: SQLAlchemy  # 数据库实例
    conversation: Conversation  # 会话模型
    model_instance: BaseLanguageModel  # LLM大语言模型

    def _count_tokens(self, messages: list[AnyMessage]) -> int:
        """使用 tiktoken 计算消息列表的 token 数量"""
        try:
            # 尝试使用 cl100k_base 编码器（适用于 GPT-3.5/GPT-4）
            encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            # 如果失败，使用默认编码器
            try:
                encoding = tiktoken.get_encoding("gpt2")
            except Exception:
                # 如果都失败，使用简单估算：1 token ≈ 4 字符
                total_chars = sum(len(str(msg.content)) for msg in messages)
                return total_chars // 4

        # 计算所有消息的 token 数
        total_tokens = 0
        for message in messages:
            # 每条消息有固定的格式开销（约 4 tokens）
            total_tokens += 4
            # 计算消息内容的 token 数
            if hasattr(message, 'content') and message.content:
                total_tokens += len(encoding.encode(str(message.content)))
        
        return total_tokens

    def get_history_prompt_messages(
        self,
        max_token_limit: int = 2000,
        message_limit: int = 10,
    ) -> list[AnyMessage]:
        """根据传递的token限制+消息条数限制获取指定会话模型的历史消息列表"""
        # 1.判断会话模型是否存在，如果不存在则直接返回空列表
        if self.conversation is None:
            return []

        # 2.查询该会话的消息列表，并且使用时间进行倒序，同时匹配答案不为空、匹配会话id、没有软删除、状态是正常
        messages = (
            self.db.session.query(Message)
            .filter(
                Message.conversation_id == self.conversation.id,
                Message.answer != "",
                Message.is_deleted == False,
                Message.status == MessageStatus.NORMAL,
            )
            .order_by(desc("created_at"))
            .limit(message_limit)
            .all()
        )
        messages = list(reversed(messages))

        # 3.将messages转换成LangChain消息列表
        prompt_messages = []
        for message in messages:
            prompt_messages.extend(
                [
                    HumanMessage(content=message.query),
                    AIMessage(content=message.answer),
                ]
            )

        # 4.调用LangChain继承的trim_messages函数剪切消息列表
        return trim_messages(
            messages=prompt_messages,
            max_tokens=max_token_limit,
            token_counter=self._count_tokens,  # 使用自定义的 token 计数函数
            strategy="last",
            start_on="human",  # 从人类消息开始
            end_on="ai",  # 以AI消息结束
        )

    def get_history_prompt_text(
        self,
        human_prefix: str = "Human",
        ai_prefix: str = "AI",
        max_token_limit: int = 2000,
        message_limit: int = 10,
    ) -> str:
        """根据传递的数据获取指定会话历史消息提示文本(短期记忆的文本形式，用于文本生成模型)"""
        # 1.根据传递的信息获取历史消息列表
        messages = self.get_history_prompt_messages(max_token_limit, message_limit)

        # 2.调用LangChain集成的get_buffer_string()函数将消息列表转换成文本
        return get_buffer_string(messages, human_prefix, ai_prefix)
