import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import tiktoken
from injector import inject
from langchain_classic.embeddings import CacheBackedEmbeddings
from langchain_community.storage import RedisStore
from langchain_core.embeddings import Embeddings
from redis import Redis
from flask import g

from internal.core.language_model.entities.model_entity import ModelType
from internal.exception import ValidateErrorException
from internal.service.language_model.language_model_service import LanguageModelService


@inject
@dataclass
class EmbeddingsService:
    """文本嵌入模型服务 - 支持多用户（基于请求上下文）"""

    _store: RedisStore
    language_model_service: LanguageModelService
    _embeddings_cache: dict[str, Embeddings]
    _cache_backed_embeddings_cache: dict[str, CacheBackedEmbeddings]

    def __init__(self, redis: Redis, language_model_service: LanguageModelService):
        """构造函数，初始化存储器"""
        self._store = RedisStore(client=redis)
        self.language_model_service = language_model_service
        self._embeddings_cache = {}
        self._cache_backed_embeddings_cache = {}

    def initialize_embeddings(
        self,
        account_id: UUID,
        model_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        使用用户配置的 API Key 初始化嵌入模型

        Args:
            account_id: 用户账号 ID
        """
        config = model_config or self.language_model_service.get_default_model_config(
            account_id,
            model_type=ModelType.EMBEDDING,
        )
        provider_name = str(config.get("provider") or "")
        model_name = str(config.get("model") or "")
        embeddings, binding = self.language_model_service.load_embeddings_model(
            provider_name,
            model_name,
            account_id,
            provider_connection_id=config.get("provider_connection_id"),
        )

        configured_fingerprint = str(config.get("fingerprint") or "")
        if configured_fingerprint.startswith("legacy_"):
            binding["fingerprint"] = configured_fingerprint
        elif (
            configured_fingerprint and configured_fingerprint != binding["fingerprint"]
        ):
            raise ValidateErrorException(
                "向量模型服务地址已变化，请重建知识库索引后再使用"
            )

        cache_key = f"{account_id}:{binding['fingerprint']}"
        g.embeddings_cache_key = cache_key
        g.embedding_model_binding = binding
        if cache_key in self._embeddings_cache:
            return binding

        self._embeddings_cache[cache_key] = embeddings

        def blake2b_key_encoder(text: str) -> str:
            return hashlib.blake2b(text.encode(), digest_size=32).hexdigest()

        # 初始化缓存嵌入
        cache_backed_embeddings = CacheBackedEmbeddings.from_bytes_store(
            embeddings,
            self._store,
            namespace=f"embedding:{binding['fingerprint']}:",
            query_embedding_cache=True,
            key_encoder=blake2b_key_encoder,
        )
        self._cache_backed_embeddings_cache[cache_key] = cache_backed_embeddings
        return binding

    @classmethod
    def calculate_token_count(cls, query: str) -> int:
        """计算传入文本的token数"""
        # 使用cl100k_base编码，这是大多数现代模型（包括GPT-3.5和GPT-4）使用的编码
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(query))

    @property
    def store(self) -> RedisStore:
        return self._store

    def _get_current_cache_key(self) -> str:
        """读取当前请求上下文绑定的向量模型缓存键。"""
        cache_key = getattr(g, "embeddings_cache_key", None)
        if not cache_key:
            raise ValidateErrorException(
                "未找到当前用户上下文。请先调用 initialize_embeddings 方法"
            )
        return cache_key

    @property
    def current_binding(self) -> dict[str, Any]:
        binding = getattr(g, "embedding_model_binding", None)
        if not binding:
            raise ValidateErrorException("未初始化当前知识库的向量模型")
        return binding

    @property
    def embeddings(self) -> Embeddings:
        """获取当前请求上下文用户的 embeddings"""
        cache_key = self._get_current_cache_key()
        if cache_key not in self._embeddings_cache:
            raise ValidateErrorException("当前向量模型未初始化")
        return self._embeddings_cache[cache_key]

    @property
    def cache_backed_embeddings(self) -> CacheBackedEmbeddings:
        """获取当前请求上下文用户的缓存 embeddings"""
        cache_key = self._get_current_cache_key()
        if cache_key not in self._cache_backed_embeddings_cache:
            raise ValidateErrorException("当前向量模型缓存未初始化")
        return self._cache_backed_embeddings_cache[cache_key]
