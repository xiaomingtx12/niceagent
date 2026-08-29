"""应用管理及应用发布配置路由注册。"""

from flask import Blueprint

from internal.handler import AppHandler


def register_app_routes(blueprint: Blueprint, handler: AppHandler) -> None:
    """注册应用 CRUD、发布配置和旧调试兼容路由。"""
    blueprint.add_url_rule("/apps", view_func=handler.get_apps_with_page)
    blueprint.add_url_rule("/apps", methods=["POST"], view_func=handler.create_app)
    blueprint.add_url_rule("/apps/<uuid:app_id>", view_func=handler.get_app)
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>", methods=["POST"], view_func=handler.update_app
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/delete", methods=["POST"], view_func=handler.delete_app
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/copy", methods=["POST"], view_func=handler.copy_app
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/draft-app-config", view_func=handler.get_draft_app_config
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/draft-app-config",
        methods=["POST"],
        view_func=handler.update_draft_app_config,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/published-subagents",
        view_func=handler.get_published_subagent_candidates,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/publish", methods=["POST"], view_func=handler.publish
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/cancel-publish",
        methods=["POST"],
        view_func=handler.cancel_publish,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/publish-histories",
        view_func=handler.get_publish_histories_with_page,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/fallback-history",
        methods=["POST"],
        view_func=handler.fallback_history_to_draft,
    )

    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/conversations/delete-debug-conversation",
        methods=["POST"],
        view_func=handler.delete_debug_conversation,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/published-config", view_func=handler.get_published_config
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/published-config/regenerate-web-app-token",
        methods=["POST"],
        view_func=handler.regenerate_web_app_token,
    )


__all__ = ["register_app_routes"]
