from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from injector import inject
from pydantic import ValidationError
from sqlalchemy import desc

from internal.exception import ForbiddenException, NotFoundException, ValidateErrorException
from internal.core.language_model.parameters import normalize_model_parameter_contract
from internal.core.language_model.entities.model_entity import ModelEntity
from internal.core.language_model import LanguageModelManager
from internal.model import Account
from internal.model.provider_api_key import ProviderApiKey
from internal.schema.language_model.provider_api_key_schema import CreateProviderApiKeyReq
from pkg.paginator import PaginatorReq, Paginator
from pkg.sqlalchemy import SQLAlchemy
from internal.service.common.base_service import BaseService


@inject
@dataclass
class ProviderApiKeyService(BaseService):
    """提供商API密钥服务"""

    db: SQLAlchemy
    language_model_manager: LanguageModelManager

    def create_provider_api_key(
        self,
        req: CreateProviderApiKeyReq,
        account: Account,
        *,
        discovered_models: list[dict[str, Any]] | None = None,
        enabled_models: list[str] | None = None,
    ) -> ProviderApiKey:
        """创建提供商API密钥"""
        base_url, api_format = self._resolve_connection(req.provider_name.data, req.base_url.data or "", req.api_format.data)
        normalized_models = self._normalize_discovered_models(discovered_models or [])
        normalized_enabled_models = self._normalize_enabled_models(
            enabled_models,
            normalized_models,
        )

        return self.create(
            ProviderApiKey,
            account_id=account.id,
            provider_name=req.provider_name.data,
            api_key=req.api_key.data,
            base_url=base_url,
            api_format=api_format,
            connection_name=req.connection_name.data.strip(),
            is_active=req.is_active.data if req.is_active.data is not None else True,
            discovered_models=normalized_models,
            enabled_models=normalized_enabled_models,
        )

    def get_provider_api_key(
        self, provider_api_key_id: UUID, account: Account
    ) -> ProviderApiKey:
        """根据ID获取提供商API密钥"""
        provider_api_key = self.get(ProviderApiKey, provider_api_key_id)
        if not provider_api_key or provider_api_key.account_id != account.id:
            raise ForbiddenException("提供商API密钥不存在或无权限")
        return provider_api_key

    def get_active_api_key_by_provider(
        self, account_id: UUID, provider_name: str
    ) -> Optional[str]:
        """根据账号ID和提供商名称获取激活的API密钥"""
        provider_api_key = (
            self.db.session.query(ProviderApiKey)
            .filter(
                ProviderApiKey.account_id == account_id,
                ProviderApiKey.provider_name == provider_name,
                ProviderApiKey.is_active == True,
            )
            .order_by(desc(ProviderApiKey.created_at))
            .first()
        )

        return provider_api_key.api_key if provider_api_key else None

    def update_provider_api_key(
        self, provider_api_key_id: UUID, account: Account, **kwargs
    ) -> ProviderApiKey:
        """更新提供商API密钥"""
        provider_api_key = self.get_provider_api_key(provider_api_key_id, account)
        base_url, api_format = self._resolve_connection(
            kwargs.get("provider_name", provider_api_key.provider_name),
            kwargs.get("base_url", provider_api_key.base_url),
            kwargs.get("api_format", provider_api_key.api_format),
        )
        kwargs.update(base_url=base_url, api_format=api_format)
        if "connection_name" in kwargs and not kwargs["connection_name"]:
            raise ValidateErrorException("服务名称不能为空")
        connection_changed = any(key in kwargs and kwargs[key] != getattr(provider_api_key, key) for key in ("provider_name", "api_key", "base_url", "api_format"))
        if connection_changed and "discovered_models" not in kwargs:
            raise ValidateErrorException("连接已变更，请重新发现模型后保存")
        if "discovered_models" in kwargs or "enabled_models" in kwargs:
            models = self._normalize_discovered_models(kwargs.get("discovered_models", provider_api_key.discovered_models) or [])
            kwargs["discovered_models"] = models
            kwargs["enabled_models"] = self._normalize_enabled_models(kwargs.get("enabled_models", provider_api_key.enabled_models), models)
        self.update(provider_api_key, **kwargs)
        return provider_api_key

    def _resolve_connection(self, provider_name: str, base_url: str, api_format: str) -> tuple[str, str]:
        """在业务边界校验提供商并固定内置服务的连接参数。"""
        provider = self.language_model_manager.get_provider(provider_name)
        try:
            url, protocol = provider.provider_entity.resolve_connection(base_url, api_format)
        except ValueError as exc:
            raise ValidateErrorException(str(exc)) from exc
        return url, protocol.value

    def update_enabled_models(
        self,
        provider_api_key_id: UUID,
        account: Account,
        enabled_models: list[str],
        discovered_models: list[dict[str, Any]] | None = None,
    ) -> ProviderApiKey:
        """更新动态模型快照的输入模态和激活状态。"""
        provider_api_key = self.get_provider_api_key(provider_api_key_id, account)
        normalized_models = self._normalize_discovered_models(
            discovered_models
            if discovered_models is not None
            else provider_api_key.discovered_models or []
        )
        normalized_enabled_models = self._normalize_enabled_models(
            enabled_models,
            normalized_models,
        )
        self.update(
            provider_api_key,
            discovered_models=normalized_models,
            enabled_models=normalized_enabled_models,
        )
        return provider_api_key

    def delete_provider_api_key(
        self, provider_api_key_id: UUID, account: Account
    ) -> ProviderApiKey:
        """删除提供商API密钥"""
        provider_api_key = self.get_provider_api_key(provider_api_key_id, account)
        self.delete(provider_api_key)
        return provider_api_key

    def get_provider_api_keys_with_page(
        self, req: PaginatorReq, account: Account
    ) -> tuple[list[ProviderApiKey], Paginator]:
        """获取提供商API密钥分页列表"""
        paginator = Paginator(db=self.db, req=req)

        provider_api_keys = paginator.paginate(
            self.db.session.query(ProviderApiKey)
            .filter(
                ProviderApiKey.account_id == account.id,
            )
            .order_by(desc("created_at"))
        )

        return provider_api_keys, paginator

    def get_all_provider_api_keys(self, account: Account) -> list[ProviderApiKey]:
        """获取用户所有的提供商API密钥"""
        return (
            self.db.session.query(ProviderApiKey)
            .filter(
                ProviderApiKey.account_id == account.id,
            )
            .all()
        )

    @staticmethod
    def _normalize_discovered_models(
        models: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """只保存可重建 ModelEntity 的动态模型字段。"""
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw_model in models:
            if not isinstance(raw_model, dict):
                continue
            model_name = raw_model.get("model_name") or raw_model.get("model")
            if not isinstance(model_name, str) or not model_name.strip():
                continue
            model_name = model_name.strip()
            if model_name in seen:
                continue
            try:
                model_payload = {**raw_model, "model": model_name}
                model_payload.pop("model_name", None)
                model = ModelEntity.model_validate(
                    {**model_payload, "is_dynamic": True},
                    by_alias=True,
                )
                normalize_model_parameter_contract(model)
                if model.context_window and (model.max_input_tokens > model.context_window or model.max_output_tokens > model.context_window):
                    raise ValueError("输入和输出上限不能超过上下文窗口")
            except (ValidationError, TypeError, ValueError):
                raise ValidateErrorException(f"模型 {model_name} 的能力或参数定义不合法")
            normalized.append(model.model_dump(mode="json", by_alias=True))
            seen.add(model_name)
        return normalized

    @staticmethod
    def _normalize_enabled_models(
        enabled_models: list[str] | None,
        discovered_models: list[dict[str, Any]],
    ) -> list[str]:
        discovered_names = [
            model_name
            for model_name in (
                (
                    raw_model.get("model_name") or raw_model.get("model")
                    if isinstance(raw_model, dict)
                    else None
                )
                for raw_model in discovered_models
            )
            if isinstance(model_name, str) and model_name
        ]
        discovered_name_set = set(discovered_names)
        selected = (
            enabled_models
            if enabled_models is not None
            else discovered_names
        )
        return [
            model_name
            for model_name in selected
            if isinstance(model_name, str) and model_name in discovered_name_set
        ]
