"""阿里云通义千问图像生成工具"""
import os
from typing import Optional, Type

import requests
from dashscope import MultiModalConversation
from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from internal.lib.helper import add_attribute


class QwenImageGenerationArgsSchema(BaseModel):
    """通义千问图像生成工具参数模型"""
    query: str = Field(description="输入应该是生成图像的文本提示(prompt)，用中文或英文描述你想要生成的图像内容")


class QwenImageGenerationTool(BaseTool):
    """阿里云通义千问图像生成工具
    
    使用阿里云DashScope API的qwen-image-max模型生成图像
    """
    name: str = "qwen_image_generation"
    description: str = "一个将文本描述转换为图像的工具。输入文本提示词，返回生成的图像URL。"
    args_schema: Type[BaseModel] = QwenImageGenerationArgsSchema
    
    model: str = "qwen-image-max"
    size: str = "1664*928"
    api_key: Optional[str] = Field(default=None, exclude=True, repr=False)

    def _run(
        self,
        query: str,
        run_manager: Optional[CallbackManagerForToolRun] = None,
    ) -> str:
        """执行图像生成
        
        Args:
            query: 图像描述提示词
            run_manager: 回调管理器
            
        Returns:
            生成的图像URL
        """
        # 显式注入优先，部署凭据与其他内置插件保持一致。
        api_key = self.api_key or os.getenv("DASHSCOPE_API_KEY")
        if not api_key:
            return "配置错误：请配置 DASHSCOPE_API_KEY 后使用图像生成。"
        
        try:
            # 构建消息
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"text": query}
                    ]
                }
            ]
            
            # 调用通义千问图像生成API
            response = MultiModalConversation.call(
                api_key=api_key,
                model=self.model,
                messages=messages,
                result_format='message',
                stream=False,
                watermark=False,
                prompt_extend=True,
                size=self.size,
                request_timeout=60,
            )
            
            # 检查响应状态
            if response.status_code != 200:
                return (
                    f"图像生成失败: status_code={response.status_code}, "
                    "请检查服务凭据、模型可用性和配额。"
                )
            
            # 提取图像URL
            if hasattr(response, 'output') and response.output:
                if hasattr(response.output, 'choices') and response.output.choices:
                    choice = response.output.choices[0]
                    if hasattr(choice, 'message') and choice.message:
                        if hasattr(choice.message, 'content') and choice.message.content:
                            for content_item in choice.message.content:
                                # content_item是字典类型
                                if isinstance(content_item, dict) and 'image' in content_item:
                                    return content_item['image']
            
            return "错误: 图像生成响应中未找到结果"
                
        except (requests.RequestException, ValueError):
            return "图像生成失败，请检查服务配置和网络后重试。"


@add_attribute("args_schema", QwenImageGenerationArgsSchema)
def qwen_image_generation(**kwargs) -> BaseTool:
    """返回通义千问图像生成的LangChain工具
    
    Args:
        **kwargs: 工具配置参数，包括:
            - model: 模型名称，默认qwen-image-max
            - size: 图像尺寸，默认1664*928
            - api_key: API密钥（可选，默认从环境变量读取）
    
    Returns:
        QwenImageGenerationTool实例
    """
    return QwenImageGenerationTool(
        args_schema=QwenImageGenerationArgsSchema,
        **kwargs
    )
