"""第三方模型目录发现与短期缓存。"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from threading import RLock
from typing import Any
from urllib.parse import urlparse

import requests

from .entities.default_model_parameter_template import (
    DEFAULT_MODEL_PARAMETER_TEMPLATE,
)
from .entities.model_entity import (
    DefaultModelParameterName,
    ModelEntity,
    ModelFeature,
    ModelParameter,
)
from .entities.provider_entity import ModelDiscoveryConfig, ProviderEntity

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CacheEntry:
    """单个供应商和 API Key 对应的发现结果。"""

    models: tuple[ModelEntity, ...]
    expires_at: float


class ModelDiscoveryClient:
    """通过供应商模型目录生成统一的 ModelEntity。"""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        self._lock = RLock()

    def discover(
        self,
        provider: ProviderEntity,
        api_key: str,
        *,
        force_refresh: bool = False,
    ) -> list[ModelEntity]:
        """发现模型；网络失败时优先返回上一次成功结果。"""
        config = provider.model_discovery
        if not config.enabled or not provider.base_url or not api_key:
            return []

        cache_key = (
            provider.name,
            hashlib.sha256(api_key.encode("utf-8")).hexdigest(),
        )
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and not force_refresh and cached.expires_at > now:
                return self._copy_models(cached.models)

        stale_models = cached.models if cached else ()
        last_error: Exception | None = None
        headers = {
            config.api_key_header: f"{config.api_key_prefix}{api_key}",
            "Accept": "application/json",
        }
        for endpoint in self._candidate_endpoints(provider, config):
            try:
                response = requests.get(
                    endpoint,
                    headers=headers,
                    timeout=config.timeout_seconds,
                )
                if response.status_code in {404, 405}:
                    continue
                response.raise_for_status()
                records = self._extract_records(response.json())
                models = tuple(
                    model
                    for model in (
                        self._build_model_entity(provider, record)
                        for record in records
                    )
                    if model is not None
                )
                with self._lock:
                    self._cache[cache_key] = _CacheEntry(
                        models=models,
                        expires_at=now + config.cache_ttl_seconds,
                    )
                return self._copy_models(models)
            except (requests.RequestException, ValueError, TypeError) as exc:
                last_error = exc

        if last_error is not None:
            logger.warning(
                "动态发现供应商模型失败 provider=%s，保留静态模型或上次发现结果",
                provider.name,
            )
        return self._copy_models(stale_models)

    @staticmethod
    def _copy_models(
        models: tuple[ModelEntity, ...] | list[ModelEntity],
    ) -> list[ModelEntity]:
        """返回独立实体，避免调用方修改缓存对象。"""
        return [model.model_copy(deep=True) for model in models]

    @staticmethod
    def _candidate_endpoints(
        provider: ProviderEntity,
        config: ModelDiscoveryConfig,
    ) -> list[str]:
        """组装并去重模型目录地址。"""
        base_url = provider.base_url.rstrip("/")
        endpoints = [config.endpoint, *config.fallback_endpoints]
        candidates: list[str] = []
        for endpoint in endpoints:
            if not endpoint:
                continue
            if urlparse(endpoint).scheme in {"http", "https"}:
                url = endpoint
            else:
                url = f"{base_url}/{endpoint.lstrip('/')}"
            if url not in candidates:
                candidates.append(url)
        return candidates

    @staticmethod
    def _extract_records(payload: Any) -> list[dict[str, Any]]:
        """兼容 OpenAI data 列表及常见 models 列表响应。"""
        if isinstance(payload, list):
            records = payload
        elif isinstance(payload, dict):
            records = payload.get("data") or payload.get("models") or []
        else:
            records = []
        return [record for record in records if isinstance(record, dict)]

    def _build_model_entity(
        self,
        provider: ProviderEntity,
        record: dict[str, Any],
    ) -> ModelEntity | None:
        """把第三方模型目录条目转换为平台实体。"""
        model_name = record.get("id") or record.get("model") or record.get("name")
        if not isinstance(model_name, str) or not model_name.strip():
            return None
        model_name = model_name.strip()

        features = list(provider.model_discovery.default_features)
        capabilities = record.get("capabilities")
        if isinstance(capabilities, dict) and capabilities.get("tools") is False:
            features = [
                feature
                for feature in features
                if feature not in {
                    ModelFeature.TOOL_CALL,
                    ModelFeature.AGENT_THOUGHT,
                }
            ]

        architecture = record.get("architecture")
        if isinstance(architecture, dict):
            modalities = architecture.get("input_modalities") or []
            if "image" in modalities and ModelFeature.IMAGE_INPUT not in features:
                features.append(ModelFeature.IMAGE_INPUT)
            if "video" in modalities and ModelFeature.VIDEO_INPUT not in features:
                features.append(ModelFeature.VIDEO_INPUT)

        metadata: dict[str, Any] = {
            "source": "discovered",
            "object": record.get("object", ""),
            "owned_by": record.get("owned_by", ""),
        }
        if isinstance(record.get("pricing"), dict):
            metadata["pricing"] = record["pricing"]

        return ModelEntity(
            model=model_name,
            label=str(record.get("name") or model_name),
            model_type=provider.model_discovery.model_type,
            is_dynamic=True,
            features=features,
            context_window=self._first_int(
                record,
                (
                    "context_window",
                    "context_length",
                    "max_context_length",
                    "input_token_limit",
                ),
                provider.model_discovery.default_context_window,
            ),
            max_output_tokens=self._first_int(
                record,
                ("max_output_tokens", "output_token_limit"),
                provider.model_discovery.default_max_output_tokens,
            ),
            attributes={
                "model": model_name,
                "base_url": provider.base_url,
            },
            parameters=self._default_parameters(),
            metadata=metadata,
        )

    @staticmethod
    def _first_int(
        record: dict[str, Any],
        keys: tuple[str, ...],
        default: int,
    ) -> int:
        """读取第一个可用的正整数模型限制。"""
        for key in keys:
            value = record.get(key)
            if isinstance(value, int) and value > 0:
                return value
        return default

    @staticmethod
    def _default_parameters() -> list[ModelParameter]:
        """为未知模型提供 OpenAI-compatible 通用参数。"""
        parameters: list[ModelParameter] = []
        for parameter_name in (
            DefaultModelParameterName.TEMPERATURE,
            DefaultModelParameterName.TOP_P,
            DefaultModelParameterName.PRESENCE_PENALTY,
            DefaultModelParameterName.FREQUENCY_PENALTY,
            DefaultModelParameterName.MAX_TOKENS,
        ):
            template = DEFAULT_MODEL_PARAMETER_TEMPLATE[parameter_name].copy()
            if parameter_name == DefaultModelParameterName.MAX_TOKENS:
                template["default"] = 4096
            parameters.append(ModelParameter(name=parameter_name.value, **template))
        return parameters


__all__ = ["ModelDiscoveryClient"]
