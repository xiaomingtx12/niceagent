import os
from typing import Any, Type

from langchain_community.utilities import GoogleSerperAPIWrapper
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from internal.lib.helper import add_attribute


class GoogleSerperArgsSchema(BaseModel):
    """谷歌SerperAPI搜索参数描述"""

    query: str = Field(description="需要检索查询的语句.")


class GoogleSerperSearchTool(BaseTool):
    """懒加载 Google Serper，避免未配置密钥时阻断整个 Agent 启动。"""

    name: str = "google_serper"
    description: str = (
        "这是一个低成本的谷歌搜索API。当你需要搜索时，可以使用该工具，"
        "该工具的输入是一个查询语句。"
    )
    args_schema: Type[BaseModel] = GoogleSerperArgsSchema

    def __init__(self, serper_api_key: str | None = None, **kwargs: Any):
        super().__init__(**kwargs)
        self._serper_api_key = serper_api_key

    def _run(self, query: str, **kwargs: Any) -> str:
        query = (query or "").strip()
        if not query:
            return "配置错误：查询语句不能为空"

        api_key = (
            self._serper_api_key
            or kwargs.get("serper_api_key")
            or os.getenv("SERPER_API_KEY")
            or ""
        ).strip()
        if not api_key:
            return (
                "配置错误：Google Serper 搜索工具未配置 SERPER_API_KEY。"
                "请在后端 .env 中配置后重启服务，或从当前 Agent 移除该工具。"
            )

        try:
            return GoogleSerperAPIWrapper(serper_api_key=api_key).run(query)
        except Exception as exc:
            error_message = str(exc).strip()
            if "timeout" in error_message.lower():
                return "搜索超时：请检查网络连接或稍后重试"
            if "connection" in error_message.lower():
                return "搜索连接失败：请检查网络连接或 SERPER_API_KEY 配置"
            return f"搜索失败：{error_message or 'Google Serper 请求失败'}"


@add_attribute("args_schema", GoogleSerperArgsSchema)
def google_serper(**kwargs) -> BaseTool:
    """谷歌Serp搜索"""
    return GoogleSerperSearchTool(
        serper_api_key=kwargs.get("serper_api_key"),
    )
