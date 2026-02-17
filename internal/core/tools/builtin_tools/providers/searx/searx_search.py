import os
from typing import Any, List, Optional, Type

from langchain_community.utilities import SearxSearchWrapper
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from internal.lib.helper import add_attribute


class SearxSearchArgsSchema(BaseModel):
    """Searx搜索参数描述"""

    query: str = Field(
        description=(
            "需要搜索的查询语句。"
            "支持中英文关键词。"
            "示例：'犬小哈教程'、'LLM应用推荐'、'北京天气'、'最新AI新闻'"
        )
    )


class SearxSearchTool(BaseTool):
    """Searx 聚合搜索工具 - 使用中国可访问的搜索引擎"""

    name: str = "searx_search"
    description: str = (
        "实时网络搜索工具，使用国内可访问的搜索引擎(Bing、夸克、搜狗、360搜索)进行聚合搜索。"
        "适用场景："
        "1. 用户询问你不知道的具体事物、人物、产品、网站、教程、品牌等"
        "2. 用户询问实时信息、最新动态、时事新闻、当前价格等"
        "3. 用户询问'什么是XXX'、'XXX怎么样'、'XXX在哪里'等问题，且XXX是你不熟悉的内容"
        "4. 用户询问需要最新数据的问题（如天气、股票、新闻等）"
        "输入：查询语句（支持中英文关键词）"
    )
    args_schema: Type[BaseModel] = SearxSearchArgsSchema

    def __init__(
        self,
        engines: Optional[List[str]] = None,
        max_results: int = 10,
        **kwargs
    ):
        """
        初始化 Searx 搜索工具
        
        Args:
            engines: 搜索引擎列表，默认使用中国可访问的引擎
            max_results: 最大返回结果数，默认 10
            **kwargs: 其他参数
        """
        super().__init__(**kwargs)
        self._wrapper = None
        self._engines = engines or ["bing", "quark", "sogou", "360search"]
        self._max_results = max_results

    def _get_wrapper(self) -> SearxSearchWrapper:
        """懒加载 SearxSearchWrapper 实例"""
        if self._wrapper is None:
            searx_host = os.getenv("SEARX_SEARCH_URL")
            if not searx_host:
                raise ValueError(
                    "SEARX_SEARCH_URL 环境变量未配置，请在 .env 文件中设置"
                )

            # 配置请求头
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/91.0.4472.124 Safari/537.36"
                )
            }

            self._wrapper = SearxSearchWrapper(
                searx_host=searx_host,
                headers=headers,
                engines=self._engines,
                k=self._max_results,
            )

        return self._wrapper

    def _check_relevance(self, result: str, query: str) -> bool:
        """
        检查搜索结果与查询的相关性
        
        Args:
            result: 搜索结果
            query: 查询语句
            
        Returns:
            bool: 是否相关
        """
        if not result or len(result) < 30:
            return False
        
        result_lower = result.lower()
        query_lower = query.lower()
        
        # 1. 如果结果中包含完整查询，认为相关
        if query_lower in result_lower:
            return True
        
        # 2. 提取查询中的关键词（去除常见停用词）
        stopwords = {"的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", 
                     "一", "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会",
                     "着", "没有", "看", "好", "自己", "这", "那", "些", "个", "中", "与",
                     "或", "等", "及", "以", "为", "而", "且", "但", "如", "若", "则",
                     "目前", "市面", "推荐", "介绍", "什么", "怎么", "如何", "哪些",
                     "？", "?", "！", "!", "，", ","}
        
        keywords = []
        for word in query.replace("？", " ").replace("?", " ").replace("，", " ").replace("、", " ").split():
            word = word.strip()
            if len(word) > 1 and word not in stopwords:
                keywords.append(word)
        
        if not keywords:
            return True  # 如果没有关键词，默认认为相关
        
        # 3. 特殊处理：LLM/AI相关查询
        llm_query_terms = ["llm", "大语言模型", "大模型", "语言模型"]
        ai_query_terms = ["ai", "人工智能", "智能", "机器学习", "深度学习"]
        app_query_terms = ["应用", "app", "软件", "工具", "平台", "产品"]
        chatbot_query_terms = ["chatgpt", "gpt", "对话", "聊天", "chat", "bot", "机器人"]
        
        # 检查查询类型
        is_llm_query = any(term in query_lower for term in llm_query_terms)
        is_ai_query = any(term in query_lower for term in ai_query_terms)
        is_app_query = any(term in query_lower for term in app_query_terms)
        is_chatbot_query = any(term in query_lower for term in chatbot_query_terms)
        
        # LLM/AI相关结果词汇（更全面）
        llm_result_terms = ["llm", "大语言模型", "大模型", "语言模型", "chatgpt", "gpt", 
                           "claude", "gemini", "文心", "通义", "讯飞", "智谱", "deepseek",
                           "kimi", "豆包", "混元", "盘古", "悟道"]
        ai_result_terms = ["ai", "人工智能", "智能", "机器学习", "深度学习", "神经网络",
                          "自然语言", "nlp"]
        app_result_terms = ["应用", "app", "软件", "工具", "平台", "产品", "系统", "服务"]
        chatbot_result_terms = ["对话", "聊天", "chat", "bot", "机器人", "助手", "问答"]
        
        # 检查结果类型
        has_llm_result = any(term in result_lower for term in llm_result_terms)
        has_ai_result = any(term in result_lower for term in ai_result_terms)
        has_app_result = any(term in result_lower for term in app_result_terms)
        has_chatbot_result = any(term in result_lower for term in chatbot_result_terms)
        
        # 4. 智能匹配：如果查询和结果的主题一致，降低关键词匹配要求
        topic_match_score = 0
        if is_llm_query and has_llm_result:
            topic_match_score += 2
        if is_ai_query and has_ai_result:
            topic_match_score += 2
        if is_app_query and has_app_result:
            topic_match_score += 1
        if is_chatbot_query and has_chatbot_result:
            topic_match_score += 2
        
        # 如果主题匹配度高，认为相关
        if topic_match_score >= 2:
            return True
        
        # 5. 检查关键词匹配度
        matched_keywords = sum(1 for kw in keywords if kw.lower() in result_lower)
        match_ratio = matched_keywords / len(keywords) if keywords else 0
        
        # 6. 根据主题匹配调整阈值
        if topic_match_score >= 1:
            return match_ratio >= 0.15  # 有一定主题匹配，15%关键词匹配即可
        else:
            return match_ratio >= 0.3  # 一般情况，30%关键词匹配

    def _format_result(self, result: str, query: str) -> str:
        """
        格式化搜索结果
        
        Args:
            result: 原始搜索结果
            query: 查询语句
            
        Returns:
            格式化后的结果
        """
        if not result or result == "No good search result found":
            return f"未找到关于 '{query}' 的相关搜索结果，请尝试使用不同的关键词"
        
        # 检查结果相关性
        if not self._check_relevance(result, query):
            return (
                f"搜索引擎返回的结果与查询 '{query}' 相关性较低。\n"
                f"建议：\n"
                f"1. 尝试使用更具体的关键词\n"
                f"2. 使用英文关键词可能获得更好的结果\n"
                f"3. 简化查询语句，去除修饰词\n\n"
                f"原始结果预览：\n{result[:300]}..."
            )
        
        # 如果结果过长，添加提示
        if len(result) > 2000:
            return result[:2000] + "\n\n... (结果已截断，显示前2000字符)"
        
        return result

    def _run(self, *args: Any, **kwargs: Any) -> str:
        """执行搜索查询"""
        try:
            query = kwargs.get("query", "")
            if not query or not query.strip():
                return "错误: 查询语句不能为空"

            # 获取 wrapper 并执行搜索
            wrapper = self._get_wrapper()
            result = wrapper.run(query.strip())

            # 格式化并返回结果
            return self._format_result(result, query)

        except ValueError as e:
            # 配置错误
            return f"配置错误: {str(e)}"
        except Exception as e:
            # 其他错误
            error_msg = str(e)
            if "timeout" in error_msg.lower():
                return f"搜索超时: 请检查网络连接或稍后重试"
            elif "connection" in error_msg.lower():
                return f"连接错误: 无法连接到 Searx 服务器，请检查 SEARX_SEARCH_URL 配置"
            else:
                return f"搜索失败: {error_msg}"


@add_attribute("args_schema", SearxSearchArgsSchema)
def searx_search(
    engines: Optional[List[str]] = None,
    max_results: int = 10,
    **kwargs
) -> BaseTool:
    """
    创建 Searx 聚合搜索工具实例
    
    使用中国可访问的搜索引擎进行聚合搜索，包括:
    - Bing (必应)
    - Quark (夸克)
    - Sogou (搜狗)
    - 360Search (360搜索)
    
    Args:
        engines: 自定义搜索引擎列表，默认使用上述四个引擎
        max_results: 最大返回结果数，默认 10
        **kwargs: 其他参数
    
    Returns:
        BaseTool: Searx 搜索工具实例
        
    Example:
        >>> tool = searx_search()
        >>> result = tool.invoke({"query": "Python编程"})
        >>> print(result)
    """
    return SearxSearchTool(engines=engines, max_results=max_results, **kwargs)
