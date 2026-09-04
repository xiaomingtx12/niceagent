import os
from typing import Optional

from injector import inject
from langchain_community.vectorstores import FAISS
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from internal.core.agent_v2.entity.tool import DATASET_RETRIEVAL_TOOL_NAME
from internal.extension.logging_extension import logger
from internal.lib.helper import combine_documents
from .embeddings_service import EmbeddingsService


@inject
class FaissService:
    """Faiss向量数据库服务 - 支持多用户（基于请求上下文）"""

    faiss: Optional[FAISS]
    embeddings_service: EmbeddingsService
    _initialized: bool

    def __init__(self, embeddings_service: EmbeddingsService):
        """构造函数，延迟初始化 Faiss 向量数据库"""
        # 1.赋值embeddings_service
        self.embeddings_service = embeddings_service
        self.faiss = None
        self._initialized = False

    def initialize_faiss(self):
        """初始化 Faiss 向量数据库（需要先初始化 embeddings_service）"""
        # 如果已经初始化过，直接返回
        if self._initialized:
            return

        # 2.获取internal路径并计算本地向量数据库的实际路径
        internal_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        faiss_vector_store_path = os.path.join(internal_path, "core", "vector_store")

        # 3.从请求上下文获取当前用户的 embeddings 实例
        user_embeddings = self.embeddings_service.embeddings

        # 4.初始化faiss向量数据库，如果不存在则创建空的
        try:
            self.faiss = FAISS.load_local(
                folder_path=faiss_vector_store_path,
                embeddings=user_embeddings,
                allow_dangerous_deserialization=True,
            )
        except Exception as e:
            logger.warning("Faiss 向量库加载失败，将创建空索引: %s", e)
            # 如果向量存储不存在或加载失败，创建一个空的FAISS实例
            # 创建一个空的FAISS向量存储
            from langchain_core.documents import Document

            documents = [Document(page_content="", metadata={})]
            self.faiss = FAISS.from_documents(
                documents=documents, embedding=user_embeddings
            )
            # 删除临时文档，保持向量存储为空
            self.faiss.delete([self.faiss.index_to_docstore_id[0]])
            # 确保目录存在
            os.makedirs(faiss_vector_store_path, exist_ok=True)

        self._initialized = True

    def convert_faiss_to_tool(self) -> BaseTool:
        """将Faiss向量数据库检索器转换成LangChain工具"""
        # 确保 faiss 已初始化
        if not self._initialized or not self.faiss:
            from internal.exception import ValidateErrorException

            raise ValidateErrorException(
                "Faiss 向量数据库未初始化。请先配置 DashScope API 密钥"
            )

        # 1.将Faiss向量数据库转换成检索器
        retrieval = self.faiss.as_retriever(
            search_type="mmr",
            search_kwargs={"k": 5, "fetch_k": 20},
        )

        # 2.构建检索链，并将检索的结果合并成字符串
        search_chain = retrieval | combine_documents

        class DatasetRetrievalInput(BaseModel):
            """知识库检索工具输入结构"""

            query: str = Field(description="知识库检索query语句，类型为字符串")

        @tool(DATASET_RETRIEVAL_TOOL_NAME, args_schema=DatasetRetrievalInput)
        def dataset_retrieval(query: str) -> str:
            """如果需要检索扩展的知识库内容，当你觉得用户的提问超过你的知识范围时，可以尝试调用该工具，输入为搜索query语句，返回数据为检索内容字符串"""
            return search_chain.invoke(query)

        return dataset_retrieval
