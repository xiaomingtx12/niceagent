from dataclasses import dataclass
from uuid import UUID

from flask import Flask
from injector import inject
from langchain_classic.retrievers import EnsembleRetriever
from langchain_core.documents import Document as LCDocument
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field
from sqlalchemy import update

from internal.core.agent_v2.entity.tool import DATASET_RETRIEVAL_TOOL_NAME
from internal.entity.dataset_entity import RetrievalStrategy, RetrievalSource
from internal.exception import NotFoundException, ValidateErrorException
from internal.model import Dataset, DatasetQuery, Segment
from pkg.sqlalchemy import SQLAlchemy
from .base_service import BaseService
from .jieba_service import JiebaService
from .vector_database_service import VectorDatabaseService
from ..lib.helper import combine_documents


@inject
@dataclass
class RetrievalService(BaseService):
    """执行知识库检索及其业务侧记录维护。

    Dataset 权限、向量模型一致性、查询记录和命中次数由本服务负责；
    Agent Core 只通过生成的检索 Tool 使用这些能力。
    """

    db: SQLAlchemy
    jieba_service: JiebaService
    vector_database_service: VectorDatabaseService

    def search_in_datasets(
        self,
        dataset_ids: list[UUID],
        query: str,
        account_id: UUID,
        retrieval_strategy: str = RetrievalStrategy.SEMANTIC,
        k: int = 4,
        score: float = 0,
        retrival_source: str = RetrievalSource.HIT_TESTING,
        source_app_id: UUID = None,
    ) -> list[LCDocument]:
        """校验知识库归属并执行检索，同时更新查询和命中统计。"""

        # 1.提取知识库列表并校验权限同时更新知识库id
        datasets = (
            self.db.session.query(Dataset)
            .filter(
                Dataset.id.in_(dataset_ids),
                Dataset.account_id == account_id,
            )
            .all()
        )
        if datasets is None or len(datasets) == 0:
            raise NotFoundException("当前无知识库可执行检索")
        dataset_ids = [dataset.id for dataset in datasets]
        fingerprints = {dataset.embedding_fingerprint for dataset in datasets}
        if "" in fingerprints:
            raise ValidateErrorException("知识库尚未配置向量模型")
        if len(fingerprints) != 1:
            raise ValidateErrorException("一次检索只能使用相同向量模型的知识库")
        binding_dataset = datasets[0]
        self.vector_database_service.embeddings_service.initialize_embeddings(
            account_id,
            binding_dataset.embedding_model_config,
        )

        # 2.构建不同种类的检索器
        from internal.core.retrievers import SemanticRetriever, FullTextRetriever

        semantic_retriever = SemanticRetriever(
            dataset_ids=dataset_ids,
            vector_store=self.vector_database_service.vector_store,
            search_kwargs={
                "k": k,
                "score_threshold": score,
            },
        )
        full_text_retriever = FullTextRetriever(
            db=self.db,
            dataset_ids=dataset_ids,
            jieba_service=self.jieba_service,
            search_kwargs={"k": k},
        )
        hybrid_retriever = EnsembleRetriever(
            retrievers=[semantic_retriever, full_text_retriever],
            weights=[0.5, 0.5],
        )

        # 3.根据不同的检索策略执行检索
        if retrieval_strategy == RetrievalStrategy.SEMANTIC:
            lc_documents = semantic_retriever.invoke(query)[:k]
        elif retrieval_strategy == RetrievalStrategy.FULL_TEXT:
            lc_documents = full_text_retriever.invoke(query)[:k]
        else:
            lc_documents = hybrid_retriever.invoke(query)[:k]

        # 4.添加知识库查询记录（只存储唯一记录，也就是一个知识库如果检索了多篇文档，也只存储一条）
        unique_dataset_ids = list(
            set(str(lc_document.metadata["dataset_id"]) for lc_document in lc_documents)
        )
        for dataset_id in unique_dataset_ids:
            self.create(
                DatasetQuery,
                dataset_id=dataset_id,
                query=query,
                source=retrival_source,
                source_app_id=source_app_id,
                created_by=account_id,
            )

        # 5.批量更新片段的命中次数，召回次数，涵盖了构建+执行语句
        with self.db.auto_commit():
            stmt = (
                update(Segment)
                .where(
                    Segment.id.in_(
                        [
                            lc_document.metadata["segment_id"]
                            for lc_document in lc_documents
                        ]
                    )
                )
                .values(hit_count=Segment.hit_count + 1)
            )
            self.db.session.execute(stmt)

        return lc_documents

    def create_langchain_tool_from_search(
        self,
        flask_app: Flask,
        dataset_ids: list[UUID],
        account_id: UUID,
        retrieval_strategy: str = RetrievalStrategy.SEMANTIC,
        k: int = 4,
        score: float = 0,
        retrival_source: str = RetrievalSource.HIT_TESTING,
        source_app_id: UUID = None,
    ) -> BaseTool:
        """把一次已确定范围的知识库检索能力包装成 LangChain Tool。"""

        class DatasetRetrievalInput(BaseModel):
            """知识库检索工具输入结构"""

            query: str = Field(description="知识库搜索query语句，类型为字符串")

        @tool(DATASET_RETRIEVAL_TOOL_NAME, args_schema=DatasetRetrievalInput)
        def dataset_retrieval(query: str) -> str:
            """如果需要搜索扩展的知识库内容，当你觉得用户的提问超过你的知识范围时，可以尝试调用该工具，输入为搜索query语句，返回数据为检索内容字符串"""
            # 1.调用search_in_datasets检索得到LangChain文档列表
            with flask_app.app_context():
                documents = self.search_in_datasets(
                    dataset_ids=dataset_ids,
                    query=query,
                    account_id=account_id,
                    retrieval_strategy=retrieval_strategy,
                    k=k,
                    score=score,
                    retrival_source=retrival_source,
                    source_app_id=source_app_id,
                )

            # 2.将LangChain文档列表转换成字符串后返回
            if len(documents) == 0:
                return "知识库内没有检索到对应内容"

            return combine_documents(documents)

        return dataset_retrieval
