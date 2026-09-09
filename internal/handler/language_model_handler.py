import io
from dataclasses import dataclass

from flask import request, send_file
from flask_login import current_user, login_required
from injector import inject
from werkzeug.datastructures import MultiDict

from internal.middleware.rate_limit_middleware import rate_limit
from internal.schema.language_model_schema import (
    DiscoverLanguageModelsReq,
    GetLanguageModelsReq,
)
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
        query = request.args.copy()
        query.setlist("model_type", [",".join(request.args.getlist("model_type"))])
        req = GetLanguageModelsReq(query)
        if not req.validate():
            return validate_error_json(req.errors)
        return success_json(
            self.language_model_service.get_language_models(
                account_id=current_user.id,
                model_types=req.model_type.data,
            )
        )

    @rate_limit(config_key="sensitive")
    @login_required
    def discover_language_models(self):
        """用户在配置 API Key 时显式发现第三方模型。"""
        payload = request.get_json(silent=True)
        req = DiscoverLanguageModelsReq(
            formdata=MultiDict(payload if isinstance(payload, dict) else {})
        )
        if not req.validate():
            return validate_error_json(req.errors)
        return success_json(
            self.language_model_service.discover_language_models(
                req.provider_name.data.strip(),
                (req.api_key.data or "").strip(),
                base_url=(req.base_url.data or "").strip() or None,
                api_format=req.api_format.data,
                provider_connection_id=req.provider_connection_id.data or None,
                account_id=current_user.id,
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
