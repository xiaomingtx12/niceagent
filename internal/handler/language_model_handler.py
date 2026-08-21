import io
from dataclasses import dataclass
from urllib.parse import urlparse

from flask import request, send_file
from flask_login import current_user, login_required
from injector import inject

from internal.middleware.rate_limit_middleware import rate_limit
from internal.core.language_model.entities.model_entity import ModelType
from internal.service import LanguageModelService
from pkg.response import success_json, validate_error_json


@inject
@dataclass
class LanguageModelHandler:
    """语言模型处理器"""

    language_model_service: LanguageModelService

    @rate_limit(config_key="lightweight")
    @login_required
    def get_language_models(self):
        """获取所有的语言模型提供商信息"""
        raw_model_types = request.args.getlist("model_type")
        model_type_values = [
            value.strip()
            for raw_value in raw_model_types
            for value in raw_value.split(",")
            if value.strip()
        ]
        try:
            model_types = (
                {ModelType(value) for value in model_type_values}
                if model_type_values
                else None
            )
        except ValueError:
            return validate_error_json(
                {
                    "model_type": [
                        "模型类型必须是: "
                        + ", ".join(model_type.value for model_type in ModelType)
                    ]
                }
            )
        return success_json(
            self.language_model_service.get_language_models(
                account_id=current_user.id,
                model_types=model_types,
            )
        )

    @rate_limit(config_key="sensitive")
    @login_required
    def discover_language_models(self):
        """用户在配置 API Key 时显式发现第三方模型。"""
        payload = request.get_json(silent=True) or {}
        provider_name = payload.get("provider_name")
        api_key = payload.get("api_key")
        base_url = payload.get("base_url")
        if not isinstance(provider_name, str) or not provider_name.strip():
            return validate_error_json({"provider_name": ["提供商名称不能为空"]})
        if not isinstance(api_key, str) or not api_key.strip():
            return validate_error_json({"api_key": ["API密钥不能为空"]})
        if base_url is not None and (
            not isinstance(base_url, str) or base_url.strip() == ""
        ):
            base_url = None
        if base_url:
            parsed_base_url = urlparse(base_url.strip())
            if (
                parsed_base_url.scheme not in {"http", "https"}
                or not parsed_base_url.netloc
            ):
                return validate_error_json(
                    {"base_url": ["模型服务地址必须是 http 或 https URL"]}
                )
        return success_json(
            self.language_model_service.discover_language_models(
                provider_name.strip(),
                api_key.strip(),
                base_url=base_url.strip() if base_url else None,
            )
        )

    @rate_limit(config_key="lightweight")
    @login_required
    def get_language_model(self, provider_name: str, model_name: str):
        """根据传递的提供商名字+模型名字获取模型详细信息"""
        return success_json(
            self.language_model_service.get_language_model(
                provider_name,
                model_name,
                account_id=current_user.id,
                provider_connection_id=request.args.get("provider_connection_id"),
            )
        )

    def get_language_model_icon(self, provider_name: str):
        """根据传递的提供者名字获取指定提供商的icon图标"""
        icon, mimetype = self.language_model_service.get_language_model_icon(
            provider_name
        )
        return send_file(io.BytesIO(icon), mimetype)
