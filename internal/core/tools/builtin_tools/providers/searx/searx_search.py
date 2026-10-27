"""SearxNG 检索保留来源，不用关键词启发式丢弃证据。"""

import json
import os
import requests
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from internal.lib.helper import add_attribute


class SearxSearchArgsSchema(BaseModel):
    """模型只提交查询词，服务地址由部署配置提供。"""

    query: str = Field(min_length=1, description="需要检索的关键词或问题，支持中英文。")


class SearxSearchTool(BaseTool):
    """执行有超时上限的搜索，返回可引用的标题、链接和摘要。"""

    name: str = "searx_search"
    description: str = "检索实时网页信息，返回标题、来源链接和摘要；摘要不能替代全文。"
    args_schema: type[BaseModel] = SearxSearchArgsSchema
    max_results: int = Field(default=5, ge=1, le=10)
    engines: list[str] | None = None

    def _run(self, query: str) -> str:
        """转换网络和响应错误，不回显可能包含部署信息的异常。"""
        host = (os.getenv("SEARX_SEARCH_URL") or "").strip()
        if not host:
            return "配置错误：请配置 SEARX_SEARCH_URL 后使用搜索。"
        if not query.strip():
            return "查询语句不能为空。"
        params = {"q": query.strip(), "format": "json"}
        if self.engines:
            params["engines"] = ",".join(self.engines)
        try:
            response = requests.get(
                host.rstrip("/") + "/search", params=params, timeout=20
            )
            response.raise_for_status()
            data = response.json()
        except requests.Timeout:
            return "搜索超时，请稍后重试。"
        except (requests.RequestException, ValueError):
            return "搜索失败，请检查 Searx 服务和 JSON 搜索配置。"
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            return "搜索服务返回了无效结果。"
        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": str(item.get("content", ""))[:2000],
            }
            for item in data["results"][: self.max_results]
            if isinstance(item, dict)
        ]
        return json.dumps(
            {"query": query.strip(), "results": results}, ensure_ascii=False
        )


@add_attribute("args_schema", SearxSearchArgsSchema)
def searx_search(**kwargs) -> BaseTool:
    """配置参数交由实例校验，初始化不访问网络。"""
    return SearxSearchTool(**kwargs)
