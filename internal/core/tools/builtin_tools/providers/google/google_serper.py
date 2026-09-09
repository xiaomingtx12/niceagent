"""按调用读取凭据的 Serper 搜索。"""

import json
import os
import requests
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from internal.lib.helper import add_attribute


class GoogleSerperArgsSchema(BaseModel):
    """模型可填写的搜索输入。"""

    query: str = Field(min_length=1, description="需要检索的关键词或问题。")


class GoogleSerperSearchTool(BaseTool):
    """返回带来源的搜索结果，未配置凭据时不阻止 Agent 初始化。"""

    name: str = "google_serper"
    description: str = "通过 Google Serper 搜索网页，返回标题、链接与摘要。"
    args_schema: type[BaseModel] = GoogleSerperArgsSchema
    serper_api_key: str | None = Field(default=None, exclude=True, repr=False)
    max_results: int = Field(default=5, ge=1, le=10)

    def _run(self, query: str) -> str:
        """只返回可处理的失败信息，避免异常携带密钥。"""
        key = (self.serper_api_key or os.getenv("SERPER_API_KEY") or "").strip()
        if not key:
            return "配置错误：请配置 SERPER_API_KEY 后使用搜索。"
        if not query.strip():
            return "查询语句不能为空。"
        try:
            response = requests.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": key},
                json={"q": query.strip(), "num": self.max_results},
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()
        except requests.Timeout:
            return "搜索超时，请稍后重试。"
        except (requests.RequestException, ValueError):
            return "搜索失败，请检查 Serper 凭据、配额和网络。"
        if not isinstance(data, dict) or not isinstance(data.get("organic", []), list):
            return "搜索服务返回了无效结果。"
        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": str(item.get("snippet", ""))[:2000],
            }
            for item in data.get("organic", [])[: self.max_results]
            if isinstance(item, dict)
        ]
        return json.dumps(
            {"query": query.strip(), "results": results}, ensure_ascii=False
        )


@add_attribute("args_schema", GoogleSerperArgsSchema)
def google_serper(**kwargs) -> BaseTool:
    """实例参数与模板参数保持一致。"""
    return GoogleSerperSearchTool(**kwargs)
