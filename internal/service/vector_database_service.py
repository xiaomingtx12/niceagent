from dataclasses import dataclass
from typing import Any

from flask import Flask
from flask_weaviate import FlaskWeaviate
from injector import inject
from langchain_core.vectorstores import VectorStoreRetriever
from langchain_weaviate import WeaviateVectorStore
from weaviate.collections import Collection

from .embeddings_service import EmbeddingsService
from ..model import Document

# 向量数据库的集合名字
COLLECTION_NAME = "Dataset"


@inject
@dataclass
class VectorDatabaseService:
    """向量数据库服务"""

    weaviate: FlaskWeaviate
    embeddings_service: EmbeddingsService

    async def _get_client(self, flask_app: Flask):
        with flask_app.app_context():
            return self.weaviate.client

    @property
    def collection_name(self) -> str:
        binding = self.embeddings_service.current_binding
        fingerprint = str(binding.get("fingerprint") or "")
        if fingerprint.startswith("legacy_"):
            return COLLECTION_NAME
        return f"{COLLECTION_NAME}_{fingerprint[:24]}"

    def get_collection(self, fingerprint: str) -> Collection[Any, Any]:
        collection_name = (
            COLLECTION_NAME
            if fingerprint.startswith("legacy_")
            else f"{COLLECTION_NAME}_{fingerprint[:24]}"
        )
        return self.weaviate.client.collections.get(collection_name)

    @property
    def vector_store(self) -> WeaviateVectorStore:
        return WeaviateVectorStore(
            client=self.weaviate.client,
            index_name=self.collection_name,
            text_key="text",
            embedding=self.embeddings_service.cache_backed_embeddings,
        )

    async def add_documents(self, documents: list[Document], **kwargs: Any):
        """往向量数据库中新增文档，将vector_store使用async进行二次封装，避免在gevent中实现事件循环错误"""
        self.vector_store.add_documents(documents, **kwargs)

    def get_retriever(self) -> VectorStoreRetriever:
        """获取检索器"""
        return self.vector_store.as_retriever()

    @property
    def collection(self) -> Collection[Any, Any]:
        return self.weaviate.client.collections.get(self.collection_name)
