import hashlib
import mimetypes
import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID
from urllib.parse import urlparse

from flask import current_app
from injector import inject

from internal.core.language_model import LanguageModelManager
from internal.core.language_model.entities.model_entity import (
    BaseLanguageModel,
    ModelEntity,
    ModelType,
)
from internal.model.provider_api_key import ProviderApiKey
from internal.exception import NotFoundException, ValidateErrorException
from internal.extension.logging_extension import logger
from langchain_openai import OpenAIEmbeddings
from langchain_core.embeddings import Embeddings
from pydantic import ValidationError
from internal.lib.helper import convert_model_to_dict
from internal.schema.app_config_schema import AppModelConfigReq
from pkg.sqlalchemy import SQLAlchemy
from .base_service import BaseService
from ..lib.api_key_helper import get_api_key_for_provider

AGENT_MODEL_TYPES = {ModelType.CHAT}


@inject
@dataclass
class LanguageModelService(BaseService):
    """根据租户可用的 Provider 配置加载实际语言模型实例。

    账号凭证、模型启用状态和 Provider 选择属于业务层；Agent Core 只接收
    已加载的 ``BaseChatModel``，不直接访问这些配置。
    """

    db: SQLAlchemy
    language_model_manager: LanguageModelManager

    def get_language_models(
        self,
        account_id: UUID | None = None,
        model_types: set[ModelType] | None = None,
    ) -> list[dict[str, Any]]:
        """读取当前账号已激活的模型，可按用途筛选。"""
        # 1.调用语言模型管理器获取提供商列表
        providers = self.language_model_manager.get_providers()

        # 一次性读取账号下的模型连接，避免按提供商重复查询数据库。
        profiles_by_provider: dict[str, list[ProviderApiKey]] = {}
        if account_id:
            profiles = (
                self.db.session.query(ProviderApiKey)
                .filter(
                    ProviderApiKey.account_id == account_id,
                    ProviderApiKey.is_active == True,
                )
                .order_by(ProviderApiKey.created_at.desc())
                .all()
            )
            for profile in profiles:
                profiles_by_provider.setdefault(profile.provider_name, []).append(
                    profile
                )

        # 2.构建连接级模型列表。同一适配器下的不同 API Key/中转地址不能合并。
        language_models = []
        for provider in providers:
            # 3.获取提供商实体和模型实体列表
            provider_entity = provider.provider_entity
            provider_profiles = profiles_by_provider.get(provider_entity.name, [])
            model_entities = self._build_enabled_model_entities(
                provider_entity.name, provider_profiles
            )
            if model_types is not None:
                model_entities = [
                    model for model in model_entities if model.model_type in model_types
                ]
            grouped_models: dict[str, list[ModelEntity]] = {}
            for model in model_entities:
                connection_id = str(
                    model.attributes.get("provider_api_key_id") or "legacy"
                )
                grouped_models.setdefault(connection_id, []).append(model)

            profile_by_id = {str(profile.id): profile for profile in provider_profiles}
            for connection_id, connection_models in grouped_models.items():
                profile = profile_by_id.get(connection_id)
                connection_name = self._get_connection_name(
                    profile,
                    provider_entity.label,
                )
                language_models.append(
                    {
                        "name": provider_entity.name,
                        "provider_name": provider_entity.name,
                        "provider_label": provider_entity.label,
                        "connection_id": (
                            "" if connection_id == "legacy" else connection_id
                        ),
                        "connection_name": connection_name,
                        "position": provider.position,
                        "label": connection_name,
                        "icon": provider_entity.icon,
                        "description": provider_entity.description,
                        "background": provider_entity.background,
                        "support_model_types": provider_entity.supported_model_types,
                        "model_discovery": {
                            "enabled": provider_entity.model_discovery.enabled,
                            "dynamic_model_count": sum(
                                model.is_dynamic for model in connection_models
                            ),
                            "static_model_count": sum(
                                not model.is_dynamic for model in connection_models
                            ),
                        },
                        "models": convert_model_to_dict(connection_models),
                    }
                )

        return language_models

    def get_language_model(
        self,
        provider_name: str,
        model_name: str,
        account_id: UUID | None = None,
        provider_connection_id: str | None = None,
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
            provider_connection_id=provider_connection_id,
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
        model_config: AppModelConfigReq | dict[str, Any],
        account_id: UUID = None,
    ) -> BaseLanguageModel:
        """校验租户模型配置并加载可执行的大语言模型实例。"""
        try:
            config = AppModelConfigReq.model_validate(model_config)
        except ValidationError as exc:
            raise ValidateErrorException(f"模型配置格式错误: {exc}") from exc
        if not account_id:
            raise ValidateErrorException("加载模型缺少账号信息")

        provider_name = config.provider
        model_name = config.model
        provider_connection_id = (
            str(config.provider_connection_id)
            if config.provider_connection_id is not None
            else None
        )
        provider = self.language_model_manager.get_provider(provider_name)
        model_entity = self.get_enabled_model_entity(
            provider_name,
            model_name,
            account_id,
            provider_connection_id=provider_connection_id,
        )
        if not model_entity:
            raise ValidateErrorException(
                f"模型 {provider_name}/{model_name} 未激活，请先在 API 密钥设置中激活该模型"
            )
        if model_entity.model_type not in AGENT_MODEL_TYPES:
            raise ValidateErrorException(
                f"模型 {provider_name}/{model_name} 不是 Agent 可调用的对话模型"
            )

        parameters = dict(config.parameters)
        api_key = None
        provider_api_key_id = model_entity.attributes.get("provider_api_key_id")
        if provider_api_key_id:
            profile = (
                self.db.session.query(ProviderApiKey)
                .filter(
                    ProviderApiKey.id == provider_api_key_id,
                    ProviderApiKey.account_id == account_id,
                    ProviderApiKey.provider_name == provider_name,
                    ProviderApiKey.is_active.is_(True),
                )
                .first()
            )
            if profile:
                api_key = profile.api_key
        if not api_key:
            api_key = get_api_key_for_provider(provider_name, account_id)
        parameters[self._get_api_key_param_name(provider_name)] = api_key

        try:
            model_class = provider.get_model_class(
                model_entity.model_type,
                model_entity=model_entity,
            )
        except ValueError as exc:
            raise ValidateErrorException(str(exc)) from exc
        model_attributes = {
            key: value
            for key, value in model_entity.attributes.items()
            if key != "provider_api_key_id"
        }
        return model_class(
            **model_attributes,
            **parameters,
            features=model_entity.features,
            input_modalities=model_entity.input_modalities,
            output_modalities=model_entity.output_modalities,
            native_file_input=model_entity.native_file_input,
            metadata=model_entity.metadata,
        )

    def get_embedding_model_binding(
        self,
        provider_name: str,
        model_name: str,
        account_id: UUID,
        provider_connection_id: str | None = None,
    ) -> dict[str, Any]:
        """校验动态向量模型，并生成稳定的向量集合标识。"""
        model_entity = self.get_enabled_model_entity(
            provider_name,
            model_name,
            account_id,
            provider_connection_id=provider_connection_id,
        )
        if not model_entity:
            raise ValidateErrorException(
                f"向量模型 {provider_name}/{model_name} 未激活"
            )
        if model_entity.model_type != ModelType.EMBEDDING:
            raise ValidateErrorException(
                f"模型 {provider_name}/{model_name} 不是向量模型"
            )
        base_url = str(model_entity.attributes.get("base_url") or "").rstrip("/")
        provider_api_key_id = str(
            model_entity.attributes.get("provider_api_key_id") or ""
        )
        fingerprint = hashlib.sha256(
            f"{provider_name}\0{provider_api_key_id}\0{base_url}\0{model_name}".encode(
                "utf-8"
            )
        ).hexdigest()
        dimension = int(
            model_entity.attributes.get("dimensions")
            or model_entity.metadata.get("dimensions")
            or 0
        )
        return {
            "provider": provider_name,
            "model": model_name,
            "base_url": base_url,
            "api_key_id": provider_api_key_id,
            "fingerprint": fingerprint,
            "dimension": dimension,
        }

    def load_embeddings_model(
        self,
        provider_name: str,
        model_name: str,
        account_id: UUID,
        provider_connection_id: str | None = None,
    ) -> tuple[Embeddings, dict[str, Any]]:
        """通过 OpenAI-compatible 接口加载已激活的向量模型。"""
        binding = self.get_embedding_model_binding(
            provider_name,
            model_name,
            account_id,
            provider_connection_id=provider_connection_id,
        )
        api_key = get_api_key_for_provider(provider_name, account_id)
        if binding["api_key_id"]:
            profile = (
                self.db.session.query(ProviderApiKey)
                .filter(
                    ProviderApiKey.id == binding["api_key_id"],
                    ProviderApiKey.account_id == account_id,
                    ProviderApiKey.provider_name == provider_name,
                    ProviderApiKey.is_active == True,
                )
                .first()
            )
            if profile:
                api_key = profile.api_key
        embeddings = OpenAIEmbeddings(
            model=model_name,
            api_key=api_key,
            base_url=binding["base_url"] or None,
            dimensions=binding["dimension"] or None,
        )
        return embeddings, binding

    def _get_api_key_param_name(self, provider_name: str) -> str:
        """根据提供商名称返回对应的 API Key 参数名（所有提供商统一使用OpenAI兼容接口，参数名均为api_key）"""
        return "api_key"

    def discover_language_models(
        self,
        provider_name: str,
        api_key: str,
        base_url: str | None = None,
    ) -> list[dict[str, Any]]:
        """显式发现一次第三方目录，供 API Key 配置流程使用。"""
        if not api_key or not api_key.strip():
            raise ValueError("API 密钥不能为空")
        models = self.language_model_manager.discover_models(
            provider_name,
            api_key.strip(),
            base_url=base_url,
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
        provider_api_key_id: str | None = None,
    ) -> list[ModelEntity]:
        """按 API Key 激活顺序读取当前账号可使用的模型。"""
        if not account_id:
            return []

        profiles = self._get_provider_profiles(
            provider_name,
            account_id,
            provider_api_key_id=provider_api_key_id,
        )
        if not profiles:
            return []

        return self._build_enabled_model_entities(provider_name, profiles)

    def _build_enabled_model_entities(
        self,
        provider_name: str,
        profiles: list[ProviderApiKey],
    ) -> list[ModelEntity]:
        """从已读取的连接快照还原启用模型，不再发起数据库查询。"""
        if not profiles:
            return []

        enabled_models: list[tuple[ProviderApiKey, str, dict[str, Any]]] = []
        seen_models: set[tuple[str, str]] = set()
        for profile in profiles:
            discovered_by_name = {
                (raw_model.get("model_name") or raw_model.get("model")): raw_model
                for raw_model in profile.discovered_models or []
                if isinstance(raw_model, dict)
                and isinstance(raw_model.get("model_name") or raw_model.get("model"), str)
            }
            for model_name in profile.enabled_models or []:
                if not isinstance(model_name, str) or not model_name:
                    continue
                model_key = (str(profile.id), model_name)
                if (
                    model_key not in seen_models
                    and model_name in discovered_by_name
                ):
                    enabled_models.append(
                        (profile, model_name, discovered_by_name[model_name])
                    )
                    seen_models.add(model_key)

        # enabled_models 是唯一的运行时白名单。快照中不存在的名称不再兜底到静态目录。
        models: list[ModelEntity] = []
        provider = self.language_model_manager.get_provider(provider_name)
        for profile, model_name, raw_model in enabled_models:
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
                    "base_url": (
                        profile.base_url.strip() or provider.provider_entity.base_url
                    ),
                    "provider_api_key_id": str(profile.id),
                }
                models.append(model)
            except ValidationError:
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
        provider_connection_id: str | None = None,
    ) -> ModelEntity | None:
        """读取当前账号已激活的指定模型。"""
        return next(
            (
                model
                for model in self.get_enabled_model_entities(
                    provider_name,
                    account_id,
                    provider_api_key_id=provider_connection_id,
                )
                if model.model_name == model_name
                and (
                    not provider_connection_id
                    or str(model.attributes.get("provider_api_key_id"))
                    == str(provider_connection_id)
                )
            ),
            None,
        )

    def get_default_model_config(
        self,
        account_id: UUID,
        model_type: ModelType = ModelType.CHAT,
    ) -> dict[str, Any]:
        """按账号已激活顺序选择指定用途的默认模型。"""
        providers = sorted(
            self.language_model_manager.get_providers(),
            key=lambda item: item.position,
        )
        for provider in providers:
            models = [
                model
                for model in self.get_enabled_model_entities(provider.name, account_id)
                if model.model_type == model_type
            ]
            if not models:
                continue
            model = models[0]
            return {
                "provider": provider.name,
                "model": model.model_name,
                "provider_connection_id": str(
                    model.attributes.get("provider_api_key_id") or ""
                ),
                "parameters": {
                    parameter.name: parameter.default for parameter in model.parameters
                },
                "features": [feature.value for feature in model.features],
                "input_modalities": [
                    modality.value for modality in model.input_modalities
                ],
                "output_modalities": [
                    modality.value for modality in model.output_modalities
                ],
                "native_file_input": model.native_file_input,
            }

        raise ValidateErrorException(
            f"当前账号没有已激活的 {model_type.value} 模型，请先完成模型发现并激活"
        )

    def _get_provider_profiles(
        self,
        provider_name: str,
        account_id: UUID | None,
        provider_api_key_id: str | None = None,
    ) -> list[ProviderApiKey]:
        connection_uuid = None
        if provider_api_key_id:
            try:
                connection_uuid = UUID(str(provider_api_key_id))
            except (TypeError, ValueError):
                return []
        query = self.db.session.query(ProviderApiKey).filter(
            ProviderApiKey.account_id == account_id,
            ProviderApiKey.provider_name == provider_name,
            ProviderApiKey.is_active == True,
        )
        if connection_uuid:
            query = query.filter(ProviderApiKey.id == connection_uuid)
        return query.order_by(ProviderApiKey.created_at.desc()).all()

    @staticmethod
    def _get_connection_name(profile: ProviderApiKey | None, provider_label: str) -> str:
        if profile and profile.remark and profile.remark.strip():
            return profile.remark.strip()
        if profile and profile.base_url:
            host = urlparse(profile.base_url).netloc
            if host:
                return f"{provider_label} · {host}"
        return provider_label
