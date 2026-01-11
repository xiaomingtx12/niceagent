from dataclasses import dataclass
from typing import List

from injector import inject
from jieba.analyse.tfidf import TFIDF

from internal.entity.jieba_entity import STOPWORD_SET


@inject
@dataclass
class JiebaService:
    """结巴分词服务"""

    # 初始化 TFIDF 实例，避免全局修改
    _tfidf: TFIDF = None

    @classmethod
    def _init_tfidf(cls):
        """初始化 TFIDF 实例并设置停用词（懒加载）"""
        if cls._tfidf is None:
            cls._tfidf = TFIDF()
            # 正确设置停用词：合并默认停用词 + 自定义停用词
            cls._tfidf.stop_words.update(STOPWORD_SET)

    @classmethod
    def extract_keywords(
        cls, text: str, max_keyword_pre_chunk: int = 10, jieba=None
    ) -> List[str]:
        """
        根据输入的文本提取关键词列表
        :param text: 待提取关键词的文本
        :param max_keyword_pre_chunk: 提取的关键词数量上限
        :return: 关键词列表
        """
        # 懒加载初始化 TFIDF（确保停用词生效）
        cls._init_tfidf()

        # 使用独立的 TFIDF 实例提取关键词，避免全局污染
        return jieba.analyse.extract_tags(
            sentence=text,
            topK=max_keyword_pre_chunk,
            allowPOS=(),  # 可根据需求添加词性过滤，如 allowPOS=('n', 'v')
            tfidf=cls._tfidf,  # 指定自定义停用词的 TFIDF 实例
        )
