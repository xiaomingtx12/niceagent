from dataclasses import dataclass
from uuid import UUID

from flask import request
from flask_login import login_required, current_user
from injector import inject

from internal.core.language_model import LanguageModelManager
from internal.middleware.rate_limit_middleware import rate_limit
from internal.schema.app_schema import (
    CreateAppReq,
    GetAppResp,
    FallbackHistoryToDraftReq,
    GetPublishHistoriesWithPageReq,
    GetPublishHistoriesWithPageResp,
    UpdateAppReq,
    GetAppsWithPageReq,
    GetAppsWithPageResp,
)
from internal.schema.app_config_schema import DraftAppConfigPatchReq
from internal.service import AppService, RetrievalService
from pkg.paginator import PageModel
from pkg.response import (
    success_message,
    validate_error_json,
    success_json,
)


@inject
@dataclass
class AppHandler(object):
    """应用控制器"""

    app_service: AppService
    retrieval_service: RetrievalService
    language_model_manager: LanguageModelManager

    @rate_limit(config_key="user")
    @login_required
    def create_app(self):
        """调用服务创建新的APP记录"""
        req = CreateAppReq()
        if not req.validate():
            return validate_error_json(req.errors)

        # 2.调用服务创建应用信息
        app = self.app_service.create_app(req, current_user)

        # 3.返回创建成功响应提示
        return success_json({"id": app.id})

    @rate_limit(config_key="lightweight")
    @login_required
    def get_app(self, app_id: UUID):
        """获取应用详情和调试工作台使用的会话 ID。"""
        app, conversation_id = self.app_service.get_app_with_debug_conversation(
            app_id,
            current_user,
        )
        resp = GetAppResp()
        return success_json(resp.dump((app, conversation_id)))

    @rate_limit(config_key="user")
    @login_required
    def update_app(self, app_id: UUID):
        """根据传递的信息更新指定的应用"""
        # 1.提取数据并校验
        req = UpdateAppReq()
        if not req.validate():
            return validate_error_json(req.errors)

        # 2.调用服务更新数据
        self.app_service.update_app(app_id, current_user, **req.data)

        return success_message("修改Agent智能体应用成功")

    @rate_limit(config_key="user")
    @login_required
    def copy_app(self, app_id: UUID):
        """根据传递的应用id快速拷贝该应用"""
        app = self.app_service.copy_app(app_id, current_user)
        return success_json({"id": app.id})

    @rate_limit(config_key="sensitive")
    @login_required
    def delete_app(self, app_id: UUID):
        """根据传递的信息删除指定的应用"""
        self.app_service.delete_app(app_id, current_user)
        return success_message("删除Agent智能体应用成功")

    @rate_limit(config_key="database_intensive")
    @login_required
    def get_apps_with_page(self):
        """获取当前登录账号的应用分页列表数据"""
        # 1.提取数据并校验
        req = GetAppsWithPageReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)

        # 2.调用服务获取列表数据以及分页器
        apps, paginator = self.app_service.get_apps_with_page(req, current_user)

        # 3.构建响应结构并返回
        resp = GetAppsWithPageResp(many=True)

        return success_json(PageModel(list=resp.dump(apps), paginator=paginator))

    @rate_limit(config_key="lightweight")
    @login_required
    def get_published_subagent_candidates(self, app_id: UUID):
        """获取当前应用可引用的同账号已发布 Agent。"""
        candidates = self.app_service.get_published_subagent_candidates(
            app_id,
            current_user,
            request.args.get("search_word", ""),
        )
        return success_json({"list": candidates})

    @rate_limit(config_key="lightweight")
    @login_required
    def get_draft_app_config(self, app_id: UUID):
        """根据传递的应用id获取应用的最新草稿配置"""
        draft_config = self.app_service.get_draft_app_config(
            app_id,
            current_user,
            request_base_url=request.url_root.rstrip("/"),
        )
        return success_json(draft_config)

    @rate_limit(config_key="user")
    @login_required
    def update_draft_app_config(self, app_id: UUID):
        """根据传递的应用id+草稿配置更新应用的最新草稿配置"""
        # 1.获取草稿请求json数据
        body = request.get_json(force=True, silent=True)
        draft_app_config = DraftAppConfigPatchReq.from_mapping(
            body if isinstance(body, dict) else {}
        )

        # 2.调用服务更新应用的草稿配置
        self.app_service.update_draft_app_config(app_id, draft_app_config, current_user)

        return success_message("更新应用草稿配置成功")

    @rate_limit(config_key="sensitive")
    @login_required
    def publish(self, app_id: UUID):
        """根据传递的应用id发布/更新特定的草稿配置信息"""
        self.app_service.publish_draft_app_config(app_id, current_user)
        return success_message("发布/更新应用配置成功")

    @rate_limit(config_key="sensitive")
    @login_required
    def cancel_publish(self, app_id: UUID):
        """根据传递的应用id，取消发布指定的应用配置信息"""
        self.app_service.cancel_publish_app_config(app_id, current_user)
        return success_message("取消发布应用配置成功")

    @rate_limit(config_key="user")
    @login_required
    def fallback_history_to_draft(self, app_id: UUID):
        """根据传递的应用id+历史配置版本id，退回指定版本到草稿中"""
        # 1.提取数据并校验
        req = FallbackHistoryToDraftReq()
        if not req.validate():
            return validate_error_json(req.errors)

        # 2.调用服务回退指定版本到草稿
        self.app_service.fallback_history_to_draft(
            app_id, req.app_config_version_id.data, current_user
        )

        return success_message("回退历史配置至草稿成功")

    @rate_limit(config_key="database_intensive")
    @login_required
    def get_publish_histories_with_page(self, app_id: UUID):
        """根据传递的应用id，获取应用发布历史列表"""
        # 1.获取请求数据并校验
        req = GetPublishHistoriesWithPageReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)

        # 2.调用服务获取分页列表数据
        app_config_versions, paginator = (
            self.app_service.get_publish_histories_with_page(app_id, req, current_user)
        )

        # 3.创建响应结构并返回
        resp = GetPublishHistoriesWithPageResp(many=True)

        return success_json(
            PageModel(list=resp.dump(app_config_versions), paginator=paginator)
        )

    # @deprecated 已由 Agent 工作台线程生命周期管理替代，暂保留兼容。
    @rate_limit(config_key="lightweight")
    @login_required
    def get_published_config(self, app_id: UUID):
        """根据传递的应用id获取应用的发布配置信息"""
        published_config = self.app_service.get_published_config(app_id, current_user)
        return success_json(published_config)

    @rate_limit(config_key="sensitive")
    @login_required
    def regenerate_web_app_token(self, app_id: UUID):
        """根据传递的应用id重新生成WebApp凭证标识"""
        token = self.app_service.regenerate_web_app_token(app_id, current_user)
        return success_json({"token": token})

    def ping(self):
        """简单的健康检查端点"""
        return success_json({"status": "ok", "message": "pong"})
