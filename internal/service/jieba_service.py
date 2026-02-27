from dataclasses import dataclass
from typing import List
import os
import warnings

# 在导入 jieba 之前抑制警告
warnings.filterwarnings('ignore', category=UserWarning, module='jieba')
warnings.filterwarnings('ignore', category=ResourceWarning, module='jieba')

from injector import inject

# 延迟导入 jieba，避免在模块加载时触发警告
_jieba_analyse = None

from internal.entity.jieba_entity import STOPWORD_SET


def _get_jieba_analyse():
    """延迟加载 jieba.analyse 模块"""
    global _jieba_analyse
    if _jieba_analyse is None:
        import jieba.analyse
        _jieba_analyse = jieba.analyse
    return _jieba_analyse


@inject
@dataclass
class JiebaService:
    """结巴分词服务"""

    # 停用词文件路径
    _stopwords_file: str = None
    _initialized: bool = False

    @classmethod
    def _init_stopwords(cls):
        """初始化停用词文件（懒加载）"""
        if not cls._initialized:
            jieba_analyse = _get_jieba_analyse()
            
            # 获取当前文件所在目录
            current_dir = os.path.dirname(os.path.abspath(__file__))
            # 构建停用词文件路径
            stopwords_path = os.path.join(
                current_dir, 
                '../core/embeddings/stopwords.txt'
            )
            stopwords_path = os.path.normpath(stopwords_path)
            
            # 如果文件不存在，创建它
            if not os.path.exists(stopwords_path):
                os.makedirs(os.path.dirname(stopwords_path), exist_ok=True)
                with open(stopwords_path, 'w', encoding='utf-8') as f:
                    for word in STOPWORD_SET:
                        f.write(f"{word}\n")
            
            # 设置停用词文件
            jieba_analyse.set_stop_words(stopwords_path)
            cls._stopwords_file = stopwords_path
            cls._initialized = True
            
    @classmethod
    def extract_keywords(
        cls, text: str, max_keyword_pre_chunk: int = 10
    ) -> List[str]:
        """
        根据输入的文本提取关键词列表
        :param text: 待提取关键词的文本
        :param max_keyword_pre_chunk: 提取的关键词数量上限
        :return: 关键词列表
        """
        # 懒加载初始化停用词
        cls._init_stopwords()
        
        jieba_analyse = _get_jieba_analyse()
        
        # 使用 jieba.analyse.extract_tags 提取关键词
        # 这个方法内部会正确管理文件资源
        return jieba_analyse.extract_tags(
            sentence=text,
            topK=max_keyword_pre_chunk,
            withWeight=False,  # 不返回权重
            allowPOS=()  # 允许所有词性
        )
