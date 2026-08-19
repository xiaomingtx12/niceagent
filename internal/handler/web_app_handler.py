from dataclasses import dataclass
from uuid import UUID

from flask import request
from flask_login import login_required, current_user
from injector import inject

from internal.middleware.rate_limit_middleware import rate_limit
from internal.schema.web_app_schema import (
    WebAppChatReq,
    GetConversationsReq,
    GetConversationsResp,
)
from internal.schema.conversation_schema import (
    GetConversationMessagesWithPageReq,
    GetConversationMessagesWithPageResp,
    UpdateConversationNameReq,
    UpdateConversationIsPinnedReq,
)
from internal.service import WebAppService
from pkg.paginator import PageModel
from pkg.response import (
    success_json,
    validate_error_json,
    success_message,
    compact_generate_response,
)


@inject
@dataclass
class WebAppHandler:
    """WebApp处理器"""

    web_app_service: WebAppService

    @rate_limit(config_key="lightweight")
    @login_required
    def get_web_app(self, token: str):
        """根据传递的token凭证标识获取WebApp基础信息"""
        # 1.调用服务根据传递的token获取应用信息（添加features模型特性）
        resp = self.web_app_service.get_web_app_info(token)

        # 2.返回成功响应
        return success_json(resp)

    @rate_limit(config_key="chat")
    @rate_limit(config_key="user")
    @login_required
    def web_app_chat(self, token: str):
        """根据传递的token+query等信息与WebApp进行对话"""
        # 1.提取请求并校验
        req = WebAppChatReq()
        if not req.validate():
            return validate_error_json(req.errors)

        # 2.嗲用服务获取对应响应内容
        response = self.web_app_service.web_app_chat(token, req, current_user)

        return compact_generate_response(response)

    @rate_limit(config_key="user")
    @login_required
    def stop_web_app_chat(self, token: str, task_id: UUID):
        """根据传递的token+task_id停止与WebApp的对话"""
        self.web_app_service.stop_web_app_chat(token, task_id, current_user)
        return success_message("停止WebApp会话成功")

    @rate_limit(config_key="database_intensive")
    @login_required
    def get_conversations(self, token: str):
        """根据传递的token+is_pinned获取指定WebApp下的所有会话列表信息"""
        # 1.提取请求并校验
        req = GetConversationsReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)

        # 2.调用服务获取会话列表
        conversations = self.web_app_service.get_conversations(
            token, req.is_pinned.data, current_user
        )

        # 3.构建响应并返回
        resp = GetConversationsResp(many=True)

        return success_json(resp.dump(conversations))

    @rate_limit(config_key="database_intensive")
    @login_required
    def get_web_app_conversation_messages_with_page(
        self,
        token: str,
        conversation_id: UUID,
    ):
        """获取当前 WebApp 用户在指定会话中的消息。"""
        req = GetConversationMessagesWithPageReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)

        messages, paginator = (
            self.web_app_service.get_web_app_conversation_messages_with_page(
                token,
                conversation_id,
                req,
                current_user,
            )
        )
        resp = GetConversationMessagesWithPageResp(many=True)
        return success_json(PageModel(list=resp.dump(messages), paginator=paginator))

    @rate_limit(config_key="sensitive")
    @login_required
    def delete_web_app_conversation(self, token: str, conversation_id: UUID):
        """删除当前 WebApp 用户在指定会话中的会话。"""
        self.web_app_service.delete_web_app_conversation(
            token,
            conversation_id,
            current_user,
        )
        return success_message("删除会话成功")

    @rate_limit(config_key="user")
    @login_required
    def update_web_app_conversation_name(self, token: str, conversation_id: UUID):
        """修改当前 WebApp 用户在指定会话中的会话名称。"""
        req = UpdateConversationNameReq()
        if not req.validate():
            return validate_error_json(req.errors)

        self.web_app_service.update_web_app_conversation(
            token,
            conversation_id,
            current_user,
            name=req.name.data,
        )
        return success_message("修改会话名称成功")

    @rate_limit(config_key="user")
    @login_required
    def update_web_app_conversation_is_pinned(self, token: str, conversation_id: UUID):
        """修改当前 WebApp 用户在指定会话中的置顶状态。"""
        req = UpdateConversationIsPinnedReq()
        if not req.validate():
            return validate_error_json(req.errors)

        self.web_app_service.update_web_app_conversation(
            token,
            conversation_id,
            current_user,
            is_pinned=req.is_pinned.data,
        )
        return success_message("修改会话置顶状态成功")
