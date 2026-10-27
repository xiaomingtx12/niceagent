"""直接查询 MediaWiki API，避免可选客户端依赖阻塞 Agent 初始化。"""

import json
from typing import Literal
import requests
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from internal.lib.helper import add_attribute


class WikipediaSearchArgsSchema(BaseModel):
    """搜索目标由模型给出，语言由应用配置。"""

    query: str = Field(min_length=1, description="需要查询的百科主题。")


class WikipediaSearchTool(BaseTool):
    """返回百科简介和原文地址，便于引用与进一步核查。"""

    name: str = "wikipedia_search"
    description: str = "查询维基百科主题简介，返回文章标题、原文链接和摘要。"
    args_schema: type[BaseModel] = WikipediaSearchArgsSchema
    lang: Literal["zh", "en"] = "zh"
    max_results: int = Field(default=3, ge=1, le=5)

    def _run(self, query: str) -> str:
        """请求有超时上限，空结果与连接失败分别返回。"""
        if not query.strip():
            return "查询语句不能为空。"
        try:
            response = requests.get(
                f"https://{self.lang}.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "format": "json",
                    "generator": "search",
                    "gsrsearch": query.strip(),
                    "gsrlimit": self.max_results,
                    "prop": "extracts|info",
                    "inprop": "url",
                    "exintro": 1,
                    "explaintext": 1,
                    "exchars": 2000,
                },
                headers={"User-Agent": "MyDify/1.0 (Wikipedia search tool)"},
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError):
            return "百科查询失败，请检查网络后重试。"
        if not isinstance(data, dict) or data.get("error"):
            return "百科服务返回了无效结果。"
        pages = data.get("query", {}).get("pages", {})
        if not isinstance(pages, dict):
            return "百科服务返回了无效结果。"
        results = [
            {
                "title": page.get("title", ""),
                "url": page.get("fullurl", ""),
                "snippet": str(page.get("extract", ""))[:2000],
            }
            for page in sorted(pages.values(), key=lambda page: page.get("index", 0))[
                : self.max_results
            ]
        ]
        return json.dumps(
            {"query": query.strip(), "results": results}, ensure_ascii=False
        )


@add_attribute("args_schema", WikipediaSearchArgsSchema)
def wikipedia_search(**kwargs) -> BaseTool:
    """目录名称与运行工具名称一致，参数在初始化时验证。"""
    return WikipediaSearchTool(**kwargs)
