from dataclasses import dataclass
from uuid import UUID

from flask import request
from flask_login import login_required, current_user
from injector import inject

from internal.middleware.rate_limit_middleware import rate_limit
from internal.schema.conversation_schema import (
    ConversationMessageResp,
    UpdateConversationNameReq,
    UpdateConversationIsPinnedReq,
)
from internal.service import ConversationService
from pkg.paginator import CursorPaginatorReq
from pkg.response import validate_error_json, success_json, success_message


@inject
@dataclass
class ConversationHandler:
    """会话处理器"""

    conversation_service: ConversationService

    @rate_limit(config_key="database_intensive")
    @login_required
    def get_messages(self, conversation_id: UUID):
        """按稳定游标返回当前账号可访问的业务 Message DTO。"""
        # 1.Handler 只解析通用游标参数。
        req = CursorPaginatorReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)

        # 2.Service 统一执行会话鉴权和消息分页。
        messages, has_more, next_before = (
            self.conversation_service.get_account_message_page(
                conversation_id=conversation_id,
                account=current_user,
                before=req.before_uuid,
                page_size=req.page_size.data,
            )
        )
        return success_json(
            {
                "messages": ConversationMessageResp(many=True).dump(messages),
                "page": {
                    "has_more": has_more,
                    "next_before": str(next_before) if next_before else None,
                },
            }
        )

    @rate_limit(config_key="sensitive")
    @login_required
    def delete_conversation(self, conversation_id: UUID):
        """根据传递的会话id删除指定的会话"""
        self.conversation_service.delete_conversation(conversation_id, current_user)

        return success_message("删除会话成功")

    @rate_limit(config_key="sensitive")
    @login_required
    def delete_message(self, conversation_id: UUID, message_id: UUID):
        """根据传递的会话id+消息id删除指定的消息"""
        self.conversation_service.delete_message(
            conversation_id, message_id, current_user
        )

        return success_message("删除会话消息成功")

    @rate_limit(config_key="lightweight")
    @login_required
    def get_conversation_name(self, conversation_id: UUID):
        """根据传递的会话id获取指定会话的名字"""
        # 1.调用服务获取会话
        conversation = self.conversation_service.get_conversation(
            conversation_id, current_user
        )

        # 2.构建响应结构并返回
        return success_json({"name": conversation.name})

    @rate_limit(config_key="user")
    @login_required
    def update_conversation_name(self, conversation_id: UUID):
        """根据传递的会话id+name更新会话名字"""
        # 1.提取请求并校验
        req = UpdateConversationNameReq()
        if not req.validate():
            return validate_error_json(req.errors)

        # 2.调用服务更新会话名字
        self.conversation_service.update_conversation(
            conversation_id, current_user, name=req.name.data
        )

        return success_message("修改会话名称成功")

    @rate_limit(config_key="user")
    @login_required
    def update_conversation_is_pinned(self, conversation_id: UUID):
        """根据传递的会话id+is_pinned更新会话的置顶状态"""
        # 1.提取请求并校验
        req = UpdateConversationIsPinnedReq()
        if not req.validate():
            return validate_error_json(req.errors)

        # 2.调用服务更新会话置顶状态
        self.conversation_service.update_conversation(
            conversation_id, current_user, is_pinned=req.is_pinned.data
        )

        return success_message("修改会话置顶状态成功")
