import hashlib
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

import tiktoken
from injector import inject
from langchain_classic.embeddings import CacheBackedEmbeddings
from langchain_community.embeddings import DashScopeEmbeddings
from langchain_community.storage import RedisStore
from langchain_core.embeddings import Embeddings
from redis import Redis
from flask import g


@inject
@dataclass
class EmbeddingsService:
    """文本嵌入模型服务 - 支持多用户（基于请求上下文）"""

    _store: RedisStore
    _embeddings_cache: dict[UUID, Embeddings]
    _cache_backed_embeddings_cache: dict[UUID, CacheBackedEmbeddings]

    def __init__(self, redis: Redis):
        """构造函数，初始化存储器"""
        self._store = RedisStore(client=redis)
        self._embeddings_cache = {}
        self._cache_backed_embeddings_cache = {}

    def initialize_embeddings(self, account_id: UUID):
        """
        使用用户配置的 API Key 初始化嵌入模型

        Args:
            account_id: 用户账号 ID
        """
        from internal.lib.api_key_helper import get_api_key_for_provider
        from internal.exception import ValidateErrorException

        # 如果该用户已经初始化过，直接返回
        if account_id in self._embeddings_cache:
            # 设置当前请求上下文的 account_id
            g.embeddings_account_id = account_id
            return

        # 获取用户配置的 tongyi API Key
        try:
            api_key = get_api_key_for_provider("tongyi", account_id)
        except Exception as e:
            raise ValidateErrorException(f"初始化嵌入模型失败: {str(e)}")

        # 初始化嵌入模型
        embeddings = DashScopeEmbeddings(
            model="text-embedding-v1", dashscope_api_key=api_key
        )
        self._embeddings_cache[account_id] = embeddings

        # 🔥 修复 SHA-1 警告：使用 BLAKE2b (更快、更安全)
        def blake2b_key_encoder(text: str) -> str:
            return hashlib.blake2b(text.encode(), digest_size=32).hexdigest()

        # 初始化缓存嵌入
        cache_backed_embeddings = CacheBackedEmbeddings.from_bytes_store(
            embeddings,
            self._store,
            key_encoder=blake2b_key_encoder,
        )
        self._cache_backed_embeddings_cache[account_id] = cache_backed_embeddings
        
        # 设置当前请求上下文的 account_id
        g.embeddings_account_id = account_id
        
        print(
            f"嵌入模型初始化完成 - 用户ID: {account_id}",
            cache_backed_embeddings,
            embeddings,
        )

    @classmethod
    def calculate_token_count(cls, query: str) -> int:
        """计算传入文本的token数"""
        # 使用cl100k_base编码，这是大多数现代模型（包括GPT-3.5和GPT-4）使用的编码
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(query))

    @classmethod
    def calculate_token_count(cls, query: str) -> int:
        """计算传入文本的token数"""
        # 使用cl100k_base编码，这是大多数现代模型（包括GPT-3.5和GPT-4）使用的编码
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(query))

    @property
    def store(self) -> RedisStore:
        return self._store

    def _get_current_account_id(self) -> UUID:
        """从请求上下文获取当前用户ID"""
        account_id = getattr(g, 'embeddings_account_id', None)
        if not account_id:
            from internal.exception import ValidateErrorException
            raise ValidateErrorException(
                "未找到当前用户上下文。请先调用 initialize_embeddings 方法"
            )
        return account_id

    @property
    def embeddings(self) -> Embeddings:
        """获取当前请求上下文用户的 embeddings"""
        account_id = self._get_current_account_id()
        if account_id not in self._embeddings_cache:
            from internal.exception import ValidateErrorException
            raise ValidateErrorException(
                f"用户 {account_id} 的嵌入模型未初始化。请先配置 DashScope API 密钥，然后调用 initialize_embeddings 方法"
            )
        return self._embeddings_cache[account_id]

    @property
    def cache_backed_embeddings(self) -> CacheBackedEmbeddings:
        """获取当前请求上下文用户的缓存 embeddings"""
        account_id = self._get_current_account_id()
        if account_id not in self._cache_backed_embeddings_cache:
            from internal.exception import ValidateErrorException
            raise ValidateErrorException(
                f"用户 {account_id} 的嵌入模型未初始化。请先配置 DashScope API 密钥，然后调用 initialize_embeddings 方法"
            )
        return self._cache_backed_embeddings_cache[account_id]
