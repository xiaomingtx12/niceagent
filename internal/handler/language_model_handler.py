import io
from dataclasses import dataclass

from flask import request, send_file
from flask_login import current_user, login_required
from injector import inject

from internal.middleware.rate_limit_middleware import rate_limit
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
        return success_json(
            self.language_model_service.get_language_models(
                account_id=current_user.id,
            )
        )

    @rate_limit(config_key="sensitive")
    @login_required
    def discover_language_models(self):
        """用户在配置 API Key 时显式发现第三方模型。"""
        payload = request.get_json(silent=True) or {}
        provider_name = payload.get("provider_name")
        api_key = payload.get("api_key")
        if not isinstance(provider_name, str) or not provider_name.strip():
            return validate_error_json({"provider_name": ["提供商名称不能为空"]})
        if not isinstance(api_key, str) or not api_key.strip():
            return validate_error_json({"api_key": ["API密钥不能为空"]})
        return success_json(
            self.language_model_service.discover_language_models(
                provider_name.strip(),
                api_key.strip(),
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
            )
        )

    def get_language_model_icon(self, provider_name: str):
        """根据传递的提供者名字获取指定提供商的icon图标"""
        icon, mimetype = self.language_model_service.get_language_model_icon(
            provider_name
        )
        return send_file(io.BytesIO(icon), mimetype)
