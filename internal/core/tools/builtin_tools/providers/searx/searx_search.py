import os

from langchain_community.tools import SearxSearchRun
from langchain_community.utilities import SearxSearchWrapper
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from internal.lib.helper import add_attribute


class SearxSearchArgsSchema(BaseModel):
    """Searx搜索参数描述"""

    query: str = Field(description="需要检索查询的语句.")


@add_attribute("args_schema", SearxSearchArgsSchema)
def searx_search(**kwargs) -> BaseTool:
    """谷歌Serp搜索"""
    return SearxSearchRun(
        name="searx_search",
        description="这是一个低成本的聚合搜索API。当你需要搜索时事的时候，可以使用该工具，该工具的输入是一个查询语句",
        wrapper=SearxSearchWrapper(searx_host=os.getenv("SEARX_SEARCH_URL")),
        args_schema=SearxSearchArgsSchema,
    )
