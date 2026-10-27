from dataclasses import dataclass
from uuid import UUID

from flask import request
from flask_login import login_required, current_user
from injector import inject

from internal.middleware.rate_limit_middleware import rate_limit
from internal.schema.language_model.provider_api_key_schema import (
    CreateProviderApiKeyReq,
    UpdateProviderApiKeyReq,
    UpdateProviderApiKeyModelsReq,
    GetProviderApiKeysResp,
)
from internal.service.language_model.provider_api_key_service import ProviderApiKeyService
from pkg.paginator import PaginatorReq, PageModel
from pkg.response import success_message, validate_error_json, success_json


@inject
@dataclass
class ProviderApiKeyHandler:
    """提供商API密钥处理器"""

    provider_api_key_service: ProviderApiKeyService

    @rate_limit(config_key="sensitive")
    @login_required
    def create_provider_api_key(self):
        """创建提供商API密钥"""
        # 1.提取请求并校验
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return validate_error_json({"request": ["请求数据格式错误"]})

        req = CreateProviderApiKeyReq(formdata=None, data=payload)
        if not req.validate():
            return validate_error_json(req.errors)

        # 2.调用服务创建密钥
        self.provider_api_key_service.create_provider_api_key(
            req,
            current_user,
            discovered_models=req.discovered_models.data,
            enabled_models=req.enabled_models.data,
        )

        return success_message("创建提供商API密钥成功")

    @rate_limit(config_key="lightweight")
    @login_required
    def get_provider_api_key(self, provider_api_key_id: UUID):
        """获取提供商API密钥详情"""
        # 1.调用服务获取密钥
        provider_api_key = self.provider_api_key_service.get_provider_api_key(
            provider_api_key_id, current_user
        )

        # 2.使用响应 DTO 完成脱敏和序列化
        return success_json(GetProviderApiKeysResp().dump(provider_api_key))

    @rate_limit(config_key="user")
    @login_required
    def update_provider_api_key(self, provider_api_key_id: UUID):
        """更新提供商API密钥"""
        # 1.提取请求并校验
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return validate_error_json({"request": ["请求数据格式错误"]})
        req = UpdateProviderApiKeyReq(formdata=None, data=payload)
        if not req.validate():
            return validate_error_json(req.errors)

        # 2.构建更新参数（只包含非空字段）
        update_data = {}
        for name in ("provider_name", "connection_name", "api_format"):
            if name in payload:
                update_data[name] = (getattr(req, name).data or "").strip()
        for name in ("discovered_models", "enabled_models"):
            if name in payload:
                update_data[name] = getattr(req, name).data
        
        # 只有当 api_key 字段存在且不为空时才更新
        if req.api_key.data and req.api_key.data.strip():
            update_data["api_key"] = req.api_key.data.strip()
        if req.base_url.data is not None:
            update_data["base_url"] = req.base_url.data.strip()
        
        # 只有当 is_active 字段在请求中存在时才更新
        if "is_active" in request.form or "is_active" in payload:
            update_data["is_active"] = req.is_active.data
        

        # 3.调用服务更新密钥
        self.provider_api_key_service.update_provider_api_key(
            provider_api_key_id, current_user, **update_data
        )

        return success_message("更新提供商API密钥成功")

    @rate_limit(config_key="user")
    @login_required
    def update_provider_api_key_models(self, provider_api_key_id: UUID):
        """更新供应商 API Key 下已激活的动态模型。"""
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return validate_error_json({"request": ["请求数据格式错误"]})
        req = UpdateProviderApiKeyModelsReq(formdata=None, data=payload)
        if not req.validate():
            return validate_error_json(req.errors)

        self.provider_api_key_service.update_enabled_models(
            provider_api_key_id,
            current_user,
            req.enabled_models.data,
            discovered_models=req.discovered_models.data,
        )
        return success_message("模型激活状态更新成功")

    @rate_limit(config_key="sensitive")
    @login_required
    def delete_provider_api_key(self, provider_api_key_id: UUID):
        """删除提供商API密钥"""
        # 1.调用服务删除密钥
        self.provider_api_key_service.delete_provider_api_key(
            provider_api_key_id, current_user
        )
        
        return success_message("删除提供商API密钥成功")

    @rate_limit(config_key="database_intensive")
    @login_required
    def get_provider_api_keys_with_page(self):
        """获取当前登录账号的提供商API密钥分页列表信息"""
        # 1.提取请求并校验
        req = PaginatorReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)

        # 2.调用服务获取数据
        provider_api_keys, paginator = (
            self.provider_api_key_service.get_provider_api_keys_with_page(
                req, current_user
            )
        )

        # 3.构建响应结构并返回
        resp = GetProviderApiKeysResp(many=True)

        return success_json(PageModel(list=resp.dump(provider_api_keys), paginator=paginator))
