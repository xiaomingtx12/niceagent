"""Token 计数工具模块"""
import os
from typing import Any

from langchain_core.messages import BaseMessage

from internal.extension.logging_extension import logger

# 设置 HuggingFace 镜像（中国加速）
if not os.getenv('HF_ENDPOINT'):
    os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'


class TokenCounter:
    """Token 计数器，提供精确的 token 计数功能"""

    @staticmethod
    def count_messages_tokens(messages: list[BaseMessage], model_name: str = "gpt-3.5-turbo") -> int:
        """
        精确计算消息列表的 token 数量
        
        Args:
            messages: 消息列表
            model_name: 模型名称，用于选择正确的 tokenizer
            
        Returns:
            token 数量
        """
        try:
            # 方案1: 尝试使用 tiktoken（适用于 OpenAI 模型）
            import tiktoken
            
            try:
                encoding = tiktoken.encoding_for_model(model_name)
            except KeyError:
                # 如果模型不存在，使用默认编码
                encoding = tiktoken.get_encoding("cl100k_base")
            
            num_tokens = 0
            for message in messages:
                # 每条消息的基础 token（格式化开销）
                num_tokens += 4  # 每条消息约 4 个 token 的格式化开销
                
                # 消息内容的 token
                content = message.content
                if isinstance(content, str):
                    num_tokens += len(encoding.encode(content))
                elif isinstance(content, list):
                    # 处理多模态内容
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            num_tokens += len(encoding.encode(item.get("text", "")))
                        elif isinstance(item, dict) and item.get("type") == "image_url":
                            # 图片大约消耗 85-170 个 token（根据分辨率）
                            num_tokens += 85
                
                # 角色名称的 token
                num_tokens += len(encoding.encode(message.type))
            
            # 每次对话的基础 token
            num_tokens += 2
            
            return num_tokens
            
        except ImportError:
            # 方案2: tiktoken 未安装，尝试使用 transformers
            logger.warning("tiktoken 未安装，尝试使用 transformers tokenizer")
            try:
                from transformers import AutoTokenizer
                
                # 尝试加载模型对应的 tokenizer
                tokenizer = AutoTokenizer.from_pretrained(
                    "gpt2",  # 使用 GPT-2 作为后备
                    local_files_only=False
                )
                
                num_tokens = 0
                for message in messages:
                    content = message.content
                    if isinstance(content, str):
                        num_tokens += len(tokenizer.encode(content))
                    elif isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                num_tokens += len(tokenizer.encode(item.get("text", "")))
                
                return num_tokens
                
            except Exception as e:
                logger.exception("transformers tokenizer 加载失败: %s", e)
                # 方案3: 使用字符估算作为最后的后备方案
                return TokenCounter._estimate_tokens_from_messages(messages)
    
    @staticmethod
    def _estimate_tokens_from_messages(messages: list[BaseMessage]) -> int:
        """
        使用字符数估算 token 数量（后备方案）
        
        Args:
            messages: 消息列表
            
        Returns:
            估算的 token 数量
        """
        total_chars = 0
        for message in messages:
            content = message.content
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        total_chars += len(item.get("text", ""))
        
        # 英文约 4 字符 = 1 token，中文约 1.5 字符 = 1 token
        # 使用 3 作为折中值
        return total_chars // 3
    
    @staticmethod
    def get_model_name_from_llm(llm: Any) -> str:
        """
        从 LLM 实例中提取模型名称
        
        Args:
            llm: LLM 实例
            
        Returns:
            模型名称
        """
        # 尝试多种方式获取模型名称
        if hasattr(llm, "model_name"):
            return llm.model_name
        elif hasattr(llm, "model"):
            return llm.model
        elif hasattr(llm, "_model_name"):
            return llm._model_name
        else:
            return "gpt-3.5-turbo"  # 默认值
