from typing import Optional, Type

from pydantic import BaseModel, Field

from internal.core.language_model.exceptions import UnsupportedModelError
from internal.lib.helper import dynamic_import
from .model_entity import (
    BaseLanguageModel,
    ModelEntity,
    ModelType,
)


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
    default_context_window: int = Field(default=0, ge=0)
    default_max_output_tokens: int = Field(default=0, ge=0)


class ProviderEntity(BaseModel):
    """模型提供商实体信息。"""

    name: str = ""
    label: str = ""
    description: str = ""
    icon: str = ""
    background: str = ""
    model_provider: str = "openai"
    base_url: str = ""
    model_discovery: ModelDiscoveryConfig = Field(
        default_factory=ModelDiscoveryConfig
    )
    supported_model_types: list[ModelType] = Field(default_factory=list)


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
            raise UnsupportedModelError("仅支持通过模型目录发现并激活的动态模型")
        if model_type not in {ModelType.CHAT, ModelType.COMPLETION}:
            raise UnsupportedModelError("该模型不是 Agent 可调用的对话模型")
        module_name = "chat" if model_type == ModelType.CHAT else "completion"
        return dynamic_import(
            f"internal.core.language_model.providers.openai.{module_name}",
            "Chat" if module_name == "chat" else "Completion",
        )
