from langchain_community.tools import DuckDuckGoSearchResults
from pydantic import BaseModel, Field

from internal.lib.helper import add_attribute


class DDGInput(BaseModel):
    """仅向模型暴露搜索词。"""
    query: str = Field(description="需要搜索的查询语句")


@add_attribute("args_schema", DDGInput)
def duckduckgo_search(**kwargs) -> DuckDuckGoSearchResults:
    """返回DuckDuckGo搜索工具"""
    return DuckDuckGoSearchResults(
        name="duckduckgo_search",
        num_results=kwargs.get("max_results", 5),
        output_format="json",
        description="一个注重隐私的搜索工具，当你需要搜索时事时可以使用该工具，工具的输入是一个查询语句",
        args_schema=DDGInput,
    )
