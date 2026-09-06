"""WebApp 非 Agent 工作台路由注册。"""

from flask import Blueprint

from internal.handler import WebAppHandler


def register_web_app_routes(blueprint: Blueprint, handler: WebAppHandler) -> None:
    """注册 WebApp 基础信息、会话列表和会话管理路由。"""
    blueprint.add_url_rule(
        "/web-apps/<string:token>",
        view_func=handler.get_web_app,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/conversations",
        methods=["POST"],
        view_func=handler.create_conversation,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/conversations",
        view_func=handler.get_conversations,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/conversations/<uuid:conversation_id>/delete",
        methods=["POST"],
        view_func=handler.delete_web_app_conversation,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/conversations/<uuid:conversation_id>/name",
        methods=["POST"],
        view_func=handler.update_web_app_conversation_name,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/conversations/<uuid:conversation_id>/is-pinned",
        methods=["POST"],
        view_func=handler.update_web_app_conversation_is_pinned,
    )


__all__ = ["register_web_app_routes"]
