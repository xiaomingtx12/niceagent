"""共享本地知识索引，每次装配独立绑定当前账号的向量模型。"""

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from injector import inject
from langchain_community.docstore.base import Docstore
from langchain_community.vectorstores import FAISS
from langchain_core.embeddings import Embeddings
from langchain_core.tools import BaseTool, tool

from internal.core.agent_v2.entity.tool import DATASET_RETRIEVAL_TOOL_NAME
from internal.lib.helper import combine_documents
from internal.schema.agent.agent_tool_schema import DatasetRetrievalToolReq


@dataclass(frozen=True)
class _SharedFaissIndex:
    """进程内只共享只读索引和文档，不保留账号模型或凭证。"""

    index: Any
    docstore: Docstore
    index_to_docstore_id: dict[int, str]


@inject
class FaissService:
    """缓存平台公共知识索引，检索器及模型绑定只属于本次装配。"""

    def __init__(self) -> None:
        """锁仅保护本地索引首次加载，不串行化模型请求和检索。"""
        self._index: _SharedFaissIndex | None = None
        self._initialized = False
        self._lock = threading.Lock()

    def convert_faiss_to_tool(self, embeddings: Embeddings) -> BaseTool:
        """在装配时捕获授权模型，工具执行不依赖线程中的 Flask 上下文。"""
        vector_store = self.initialize_faiss(embeddings)
        retrieval = None
        if vector_store is not None:
            retrieval = vector_store.as_retriever(
                search_type="mmr",
                search_kwargs={"k": 5, "fetch_k": 20},
            )

        @tool(DATASET_RETRIEVAL_TOOL_NAME, args_schema=DatasetRetrievalToolReq)
        def dataset_retrieval(query: str) -> str:
            """如果需要检索扩展的知识库内容，当你觉得用户的提问超过你的知识范围时，可以尝试调用该工具，输入为搜索query语句，返回数据为检索内容字符串"""
            if retrieval is None:
                return ""
            return combine_documents(retrieval.invoke(query))

        return dataset_retrieval

    def initialize_faiss(self, embeddings: Embeddings) -> FAISS | None:
        """为当前模型创建独立检索包装，绝不修改其他运行持有的模型绑定。"""
        with self._lock:
            if not self._initialized:
                self._index = self._load_index(embeddings)
                self._initialized = True
            shared = self._index
        if shared is None:
            return None
        return FAISS(
            embedding_function=embeddings,
            index=shared.index,
            docstore=shared.docstore,
            index_to_docstore_id=shared.index_to_docstore_id,
        )

    @staticmethod
    def _load_index(embeddings: Embeddings) -> _SharedFaissIndex | None:
        """只加载项目自身维护的索引；尚未导入时返回空检索，损坏时保留原异常。"""
        path = Path(__file__).resolve().parents[2] / "core" / "vector_store"
        if not (path / "index.faiss").exists() and not (path / "index.pkl").exists():
            return None
        loaded = FAISS.load_local(
            folder_path=str(path),
            embeddings=embeddings,
            allow_dangerous_deserialization=True,
        )
        return _SharedFaissIndex(
            index=loaded.index,
            docstore=loaded.docstore,
            index_to_docstore_id=loaded.index_to_docstore_id,
        )
