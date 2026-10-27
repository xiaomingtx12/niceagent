from dataclasses import dataclass
from uuid import UUID

from flask import current_app, request
from injector import inject
from wechatpy import parse_message

from internal.service import WechatService


# @deprecated 微信公众号入口已不再作为主流程使用，仅保留兼容。
@inject
@dataclass
class WechatHandler:
    """[已废弃] 微信公众号旧入口适配器。"""

    wechat_service: WechatService

    def wechat(self, app_id: UUID):
        """Agent微信API校验与消息推送"""
        return self.wechat_service.wechat(
            app_id,
            method=request.method,
            message=parse_message(request.data),
            query={
                key: request.args.get(key)
                for key in ("signature", "timestamp", "nonce", "echostr")
            },
            flask_app=current_app._get_current_object(),
        )
