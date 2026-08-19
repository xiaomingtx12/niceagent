import logging
import mimetypes
import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from flask import current_app
from injector import inject

from internal.core.language_model import LanguageModelManager
from internal.core.language_model.entities.model_entity import (
    BaseLanguageModel,
    ModelEntity,
)
from internal.model.provider_api_key import ProviderApiKey
from internal.exception import NotFoundException, ValidateErrorException
from internal.lib.helper import convert_model_to_dict
from pkg.sqlalchemy import SQLAlchemy
from .base_service import BaseService
from ..lib.api_key_helper import get_api_key_for_provider

logger = logging.getLogger(__name__)


@inject
@dataclass
class LanguageModelService(BaseService):
    """语言模型服务"""

    db: SQLAlchemy
    language_model_manager: LanguageModelManager

    def get_language_models(
        self,
        account_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        """读取当前账号已激活的模型，不访问第三方目录。"""
        # 1.调用语言模型管理器获取提供商列表
        providers = self.language_model_manager.get_providers()

        # 2.构建语言模型列表，循环读取数据
        language_models = []
        for provider in providers:
            # 3.获取提供商实体和模型实体列表
            provider_entity = provider.provider_entity
            model_entities = self.get_enabled_model_entities(
                provider_entity.name,
                account_id,
            )
            if not model_entities:
                continue

            # 4.构建响应字典结构
            language_model = {
                "name": provider_entity.name,
                "position": provider.position,
                "label": provider_entity.label,
                "icon": provider_entity.icon,
                "description": provider_entity.description,
                "background": provider_entity.background,
                "support_model_types": provider_entity.supported_model_types,
                "model_discovery": {
                    "enabled": provider_entity.model_discovery.enabled,
                    "dynamic_model_count": sum(
                        model.is_dynamic for model in model_entities
                    ),
                    "static_model_count": sum(
                        not model.is_dynamic for model in model_entities
                    ),
                },
                "models": convert_model_to_dict(model_entities),
            }
            language_models.append(language_model)

        return language_models

    def get_language_model(
        self,
        provider_name: str,
        model_name: str,
        account_id: UUID | None = None,
    ) -> dict[str, Any]:
        """根据传递的提供者名字+模型名字获取模型详细信息"""
        # 1.获取提供者+模型实体信息
        provider = self.language_model_manager.get_provider(provider_name)
        if not provider:
            raise NotFoundException("该服务提供者不存在")

        # 2.获取模型实体
        model_entity = self.get_enabled_model_entity(
            provider_name,
            model_name,
            account_id,
        )
        if not model_entity:
            raise NotFoundException("该模型未激活，请先在 API 密钥设置中激活该模型")

        return convert_model_to_dict(model_entity)

    def get_language_model_icon(self, provider_name: str) -> tuple[bytes, str]:
        """根据传递的提供者名字获取提供商对应的图标信息"""
        # 1.获取提供者信息
        provider = self.language_model_manager.get_provider(provider_name)
        if not provider:
            raise NotFoundException("该服务提供者不存在")

        # 2.获取项目的根路径信息
        root_path = os.path.dirname(os.path.dirname(current_app.root_path))

        # 3.拼接得到提供者所在的文件夹
        provider_path = os.path.join(
            root_path,
            "internal",
            "core",
            "language_model",
            "providers",
            provider_name,
        )

        # 4.拼接得到icon对应的路径
        icon_path = os.path.join(provider_path, "_asset", provider.provider_entity.icon)

        # 5.检测icon是否存在
        if not os.path.exists(icon_path):
            raise NotFoundException(f"该模型提供者_asset下未提供图标")

        # 6.读取icon的类型
        mimetype, _ = mimetypes.guess_type(icon_path)
        mimetype = mimetype or "application/octet-stream"

        # 7.读取icon的字节数据
        with open(icon_path, "rb") as f:
            byte_data = f.read()
            return byte_data, mimetype

    def load_language_model(
        self,
        model_config: dict[str, Any],
        account_id: UUID = None,
    ) -> BaseLanguageModel:
        """根据传递的模型配置加载大语言模型，并返回其实例"""
        try:
            # 1.从model_config中提取出provider、model、parameters
            provider_name = model_config.get("provider", "")
            model_name = model_config.get("model", "")
            parameters = model_config.get("parameters", {}).copy()
            if not account_id:
                raise ValidateErrorException("加载模型缺少账号信息")

            provider = self.language_model_manager.get_provider(provider_name)
            model_entity = self.get_enabled_model_entity(
                provider_name,
                model_name,
                account_id,
            )
            if not model_entity:
                raise ValidateErrorException(
                    f"模型 {provider_name}/{model_name} 未激活，请先在 API 密钥设置中激活该模型"
                )

            # 2.获取用户的 API Key（必须成功，否则抛出异常）
            api_key = get_api_key_for_provider(provider_name, account_id)
            # 根据不同提供商设置对应的 API Key 参数名
            api_key_param = self._get_api_key_param_name(provider_name)
            parameters[api_key_param] = api_key

            # 3.从模型管理器获取提供者、模型实体、模型类
            model_class = provider.get_model_class(
                model_entity.model_type,
                model_entity=model_entity,
            )

            # 4.实例化模型后并返回
            return model_class(
                **model_entity.attributes,
                **parameters,
                features=model_entity.features,
                metadata=model_entity.metadata,
            )
        except Exception as e:
            # 记录错误日志
            logger.error(f"加载语言模型失败: {str(e)}")
            # 重新抛出异常，不再降级
            raise

    def _get_api_key_param_name(self, provider_name: str) -> str:
        """根据提供商名称返回对应的 API Key 参数名（所有提供商统一使用OpenAI兼容接口，参数名均为api_key）"""
        return "api_key"

    def discover_language_models(
        self,
        provider_name: str,
        api_key: str,
    ) -> list[dict[str, Any]]:
        """显式发现一次第三方目录，供 API Key 配置流程使用。"""
        if not api_key or not api_key.strip():
            raise ValueError("API 密钥不能为空")
        models = self.language_model_manager.discover_models(
            provider_name,
            api_key.strip(),
        )
        return convert_model_to_dict(models)

    def _get_enabled_discovered_models(
        self,
        provider_name: str,
        account_id: UUID | None,
    ) -> list[ModelEntity]:
        """兼容旧调用名，读取账号当前已激活的模型快照。"""
        return self.get_enabled_model_entities(provider_name, account_id)

    def get_enabled_model_entities(
        self,
        provider_name: str,
        account_id: UUID | None,
    ) -> list[ModelEntity]:
        """按 API Key 激活顺序读取当前账号可使用的模型。"""
        if not account_id:
            return []

        profile = (
            self.db.session.query(ProviderApiKey)
            .filter(
                ProviderApiKey.account_id == account_id,
                ProviderApiKey.provider_name == provider_name,
                ProviderApiKey.is_active == True,
            )
            .first()
        )
        if not profile:
            return []

        enabled_names = [
            model_name
            for model_name in profile.enabled_models or []
            if isinstance(model_name, str) and model_name
        ]
        discovered_by_name: dict[str, dict[str, Any]] = {}
        for raw_model in profile.discovered_models or []:
            if not isinstance(raw_model, dict):
                continue
            model_name = raw_model.get("model_name") or raw_model.get("model")
            if isinstance(model_name, str) and model_name:
                discovered_by_name[model_name] = raw_model

        # enabled_models 是唯一的运行时白名单。快照中不存在的名称不再兜底到静态目录。
        models: list[ModelEntity] = []
        provider = self.language_model_manager.get_provider(provider_name)
        for model_name in enabled_names:
            raw_model = discovered_by_name.get(model_name)
            if raw_model is None:
                continue
            try:
                model_payload = {**raw_model, "model": model_name}
                model_payload.pop("model_name", None)
                model = ModelEntity.model_validate(
                    {**model_payload, "is_dynamic": True},
                    by_alias=True,
                )
                model.attributes = {
                    **(model.attributes or {}),
                    "model": model_name,
                    "base_url": provider.provider_entity.base_url,
                }
                models.append(model)
            except Exception:
                logger.warning(
                    "忽略无法还原的动态模型快照 provider=%s model=%s",
                    provider_name,
                    model_name,
                )
        return models

    def get_enabled_model_entity(
        self,
        provider_name: str,
        model_name: str,
        account_id: UUID | None,
    ) -> ModelEntity | None:
        """读取当前账号已激活的指定模型。"""
        return next(
            (
                model
                for model in self.get_enabled_model_entities(provider_name, account_id)
                if model.model_name == model_name
            ),
            None,
        )

    def get_default_model_config(self, account_id: UUID) -> dict[str, Any]:
        """按账号已激活顺序选择系统默认模型配置。"""
        providers = sorted(
            self.language_model_manager.get_providers(),
            key=lambda item: item.position,
        )
        for provider in providers:
            models = self.get_enabled_model_entities(provider.name, account_id)
            if not models:
                continue
            model = models[0]
            return {
                "provider": provider.name,
                "model": model.model_name,
                "parameters": {
                    parameter.name: parameter.default
                    for parameter in model.parameters
                },
                "features": [feature.value for feature in model.features],
            }

        raise ValidateErrorException(
            "当前账号没有已激活的模型，请先在 API 密钥设置中发现并激活模型"
        )

