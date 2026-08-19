import os.path
from typing import Any, Optional, Type

import yaml
from injector import inject, singleton
from pydantic import BaseModel, Field, PrivateAttr, model_validator

from internal.exception import NotFoundException
from .entities.model_entity import BaseLanguageModel, ModelEntity, ModelType
from .entities.provider_entity import Provider, ProviderEntity
from .model_discovery import ModelDiscoveryClient


@inject
@singleton
class LanguageModelManager(BaseModel):
    """语言模型管理器"""
    provider_map: dict[str, Provider] = Field(default_factory=dict)  # 服务提供者映射
    _discovery_client: ModelDiscoveryClient = PrivateAttr(
        default_factory=ModelDiscoveryClient
    )

    @model_validator(mode="before")
    def validate_language_model_manager(cls, values: dict[str, Any]) -> dict[str, Any]:
        """使用pydantic提供的预设规则校验提供者映射，完成语言模型管理器的初始化"""
        # 1.获取当前类所在的路径
        current_path = os.path.abspath(__file__)
        providers_path = os.path.join(os.path.dirname(current_path), "providers")
        providers_yaml_path = os.path.join(providers_path, "providers.yaml")

        # 2.读取providers.yaml数据配置获取提供者列表
        with open(providers_yaml_path, encoding="utf-8") as f:
            providers_yaml_data = yaml.safe_load(f)

        # 3.循环读取服务提供者数据并配置模型信息
        values = values or {}
        values["provider_map"] = {}
        for index, provider_yaml_data in enumerate(providers_yaml_data):
            # 4.获取提供者实体数据结构，并构建服务提供者实体
            provider_entity = ProviderEntity(**provider_yaml_data)
            values["provider_map"][provider_entity.name] = Provider(
                name=provider_entity.name,
                position=index + 1,
                provider_entity=provider_entity,
            )
        return values

    def get_provider(self, provider_name: str) -> Optional[Provider]:
        """根据传递的提供者名字获取提供者"""
        provider = self.provider_map.get(provider_name, None)
        if provider is None:
            raise NotFoundException("该模型服务提供商不存在，请核实后重试")
        return provider

    def get_providers(self) -> list[Provider]:
        """获取所有提供者列表信息"""
        return list(self.provider_map.values())

    def get_model_entities(
        self,
        provider_name: str,
        *,
        discovered_models: list[ModelEntity] | None = None,
    ) -> list[ModelEntity]:
        """获取静态模型并合并已持久化的动态模型快照。"""
        provider = self.get_provider(provider_name)
        static_models = provider.get_model_entities()
        dynamic_by_name = {
            model.model_name: model
            for model in (discovered_models or [])
        }
        models = [
            dynamic_by_name.pop(model.model_name, model)
            for model in static_models
        ]
        models.extend(dynamic_by_name.values())
        return models

    def discover_models(
        self,
        provider_name: str,
        api_key: str,
    ) -> list[ModelEntity]:
        """仅在用户显式请求时访问第三方模型目录。"""
        provider = self.get_provider(provider_name)
        return self._discovery_client.discover(
            provider.provider_entity,
            api_key,
            force_refresh=True,
        )

    def get_model_entity(
        self,
        provider_name: str,
        model_name: str,
        *,
        discovered_models: list[ModelEntity] | None = None,
    ) -> ModelEntity:
        """按供应商和模型名获取静态或已持久化的动态模型实体。"""
        for model in self.get_model_entities(
            provider_name,
            discovered_models=discovered_models,
        ):
            if model.model_name == model_name:
                return model
        raise NotFoundException("该模型实体不存在，请核实后重试")

    def get_model_class_by_provider_and_type(
        self,
        provider_name: str,
        model_type: ModelType,
    ) -> Optional[Type[BaseLanguageModel]]:
        """根据传递的提供者名字+模型类型，获取模型类"""
        provider = self.get_provider(provider_name)

        return provider.get_model_class(model_type)

    def get_model_class_by_provider_and_model(
        self,
        provider_name: str,
        model_name: str,
        discovered_models: list[ModelEntity] | None = None,
    ) -> Optional[Type[BaseLanguageModel]]:
        """根据传递的提供者名字+模型名字获取模型类"""
        # 1.根据名字获取提供者信息
        provider = self.get_provider(provider_name)

        # 2.在提供者下获取该模型实体
        model_entity = self.get_model_entity(
            provider_name,
            model_name,
            discovered_models=discovered_models,
        )

        return provider.get_model_class(
            model_entity.model_type,
            model_entity=model_entity,
        )
