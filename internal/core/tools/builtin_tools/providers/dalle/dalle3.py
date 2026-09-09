"""兼容既有 DALL-E 3 绑定，调用时才初始化付费服务客户端。"""

import os
from openai import OpenAI, OpenAIError
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from internal.lib.helper import add_attribute


class Dalle3ArgsSchema(BaseModel):
    """模型只提供图像描述，不接触凭据。"""

    query: str = Field(min_length=1, description="需要生成图像的文本提示。")


class Dalle3Tool(BaseTool):
    """让运行名称与目录、Skill 授权和子 Agent 引用一致。"""

    name: str = "dalle3"
    description: str = "使用 DALL-E 3 将文本描述生成图像，返回图像链接。"
    args_schema: type[BaseModel] = Dalle3ArgsSchema
    size: str = "1024x1024"
    style: str = "vivid"
    api_key: str | None = Field(default=None, exclude=True, repr=False)

    def _run(self, query: str) -> str:
        """客户端初始化失败只影响本次调用，不影响其他能力装配。"""
        key = self.api_key or os.getenv("OPENAI_API_KEY")
        if not key:
            return "配置错误：请配置 OPENAI_API_KEY 后使用图像生成。"
        try:
            with OpenAI(api_key=key, max_retries=1, timeout=60) as client:
                response = client.images.generate(
                    model="dall-e-3",
                    prompt=query,
                    size=self.size,
                    style=self.style,
                    n=1,
                    response_format="url",
                )
                return (
                    "\n".join(item.url for item in response.data or [] if item.url)
                    or "图像生成未返回结果。"
                )
        except (OpenAIError, ValueError):
            return "图像生成失败，请检查服务凭据、模型可用性和配额。"


@add_attribute("args_schema", Dalle3ArgsSchema)
def dalle3(**kwargs) -> BaseTool:
    """保留原参数与 openai_api_key 别名。"""
    if "openai_api_key" in kwargs:
        kwargs["api_key"] = kwargs.pop("openai_api_key")
    return Dalle3Tool(**kwargs)
