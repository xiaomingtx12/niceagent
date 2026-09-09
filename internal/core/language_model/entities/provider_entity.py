from typing import Optional, Type
from enum import Enum
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from internal.lib.helper import dynamic_import
from .model_entity import (
    BaseLanguageModel,
    ModelEntity,
    ModelType,
)


class ApiFormat(str, Enum):
    """连接协议决定模型请求路径和参数映射。"""

    CHAT_COMPLETIONS = "chat_completions"
    RESPONSES = "responses"


class ModelDiscoveryConfig(BaseModel):
    """第三方模型目录发现配置。"""

    enabled: bool = False
    endpoint: str = "/models"
    fallback_endpoints: list[str] = Field(
        default_factory=lambda: ["/v1/models"]
    )
    timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    cache_ttl_seconds: int = Field(default=300, ge=0, le=86400)
    api_key_header: str = "Authorization"
    api_key_prefix: str = "Bearer "
    model_type: ModelType = ModelType.CHAT


class ProviderEntity(BaseModel):
    """模型提供商实体信息。"""

    name: str = ""
    label: str = ""
    description: str = ""
    icon: str = ""
    background: str = ""
    base_url: str = ""
    api_format: ApiFormat = ApiFormat.CHAT_COMPLETIONS
    model_discovery: ModelDiscoveryConfig = Field(
        default_factory=ModelDiscoveryConfig
    )
    supported_model_types: list[ModelType] = Field(default_factory=list)

    def resolve_connection(self, base_url: str = "", api_format: str = "chat_completions") -> tuple[str, ApiFormat]:
        """内置服务固定协议与端点，自定义连接必须显式提供服务根地址。"""
        if self.name != "custom":
            return self.base_url, self.api_format
        url = base_url.strip().rstrip("/")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("自定义端点必须是有效的 HTTP(S) 服务根地址")
        if parsed.path.endswith(("/chat/completions", "/responses", "/models")):
            raise ValueError("请填写服务根地址，不要包含 /chat/completions、/responses 或 /models")
        return url, ApiFormat(api_format)


class Provider(BaseModel):
    """模型服务提供商；具体模型只来自动态发现快照。"""

    name: str
    position: int
    provider_entity: ProviderEntity

    def get_model_class(
        self,
        model_type: ModelType,
        model_entity: ModelEntity | None = None,
    ) -> Optional[Type[BaseLanguageModel]]:
        """动态模型统一使用 OpenAI-compatible LangChain 适配器。"""
        if model_entity is None or not model_entity.is_dynamic:
            raise ValueError("仅支持通过模型目录发现并激活的动态模型")
        if model_type not in {ModelType.CHAT, ModelType.COMPLETION}:
            raise ValueError("该模型不是 Agent 可调用的对话模型")
        module_name = "chat" if model_type == ModelType.CHAT else "completion"
        return dynamic_import(
            f"internal.core.language_model.providers.openai.{module_name}",
            "Chat" if module_name == "chat" else "Completion",
        )
