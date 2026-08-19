from dataclasses import dataclass
from typing import Any, Optional
from uuid import UUID

from injector import inject
from sqlalchemy import desc

from internal.exception import ForbiddenException, NotFoundException
from internal.core.language_model.entities.model_entity import ModelEntity
from internal.model import Account
from internal.model.provider_api_key import ProviderApiKey
from internal.schema.provider_api_key_schema import CreateProviderApiKeyReq
from pkg.paginator import PaginatorReq, Paginator
from pkg.sqlalchemy import SQLAlchemy
from .base_service import BaseService


@inject
@dataclass
class ProviderApiKeyService(BaseService):
    """提供商API密钥服务"""

    db: SQLAlchemy

    def create_provider_api_key(
        self,
        req: CreateProviderApiKeyReq,
        account: Account,
        *,
        discovered_models: list[dict[str, Any]] | None = None,
        enabled_models: list[str] | None = None,
    ) -> ProviderApiKey:
        """创建提供商API密钥"""
        # 检查该用户是否已经配置了该提供商的密钥
        existing = (
            self.db.session.query(ProviderApiKey)
            .filter(
                ProviderApiKey.account_id == account.id,
                ProviderApiKey.provider_name == req.provider_name.data,
            )
            .first()
        )

        if existing:
            raise ForbiddenException(f"已存在 {req.provider_name.data} 的API密钥配置")

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
            is_active=req.is_active.data if req.is_active.data is not None else True,
            remark=req.remark.data or "",
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
            .first()
        )

        return provider_api_key.api_key if provider_api_key else None

    def update_provider_api_key(
        self, provider_api_key_id: UUID, account: Account, **kwargs
    ) -> ProviderApiKey:
        """更新提供商API密钥"""
        provider_api_key = self.get_provider_api_key(provider_api_key_id, account)
        if "api_key" in kwargs:
            kwargs["discovered_models"] = []
            kwargs["enabled_models"] = []
        self.update(provider_api_key, **kwargs)
        return provider_api_key

    def update_enabled_models(
        self,
        provider_api_key_id: UUID,
        account: Account,
        enabled_models: list[str],
    ) -> ProviderApiKey:
        """更新当前供应商配置中已激活的动态模型。"""
        provider_api_key = self.get_provider_api_key(provider_api_key_id, account)
        normalized_enabled_models = self._normalize_enabled_models(
            enabled_models,
            provider_api_key.discovered_models or [],
        )
        self.update(
            provider_api_key,
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
            except Exception:
                continue
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
