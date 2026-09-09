"""Agent 工作台路由注册。"""

from flask import Blueprint

from internal.handler import (
    AgentWorkbenchHandler,
    AssistantAgentProtocolHandler,
    InternalAgentProtocolHandler,
    OpenAPIAgentProtocolHandler,
)


def register_agent_workbench_routes(
    blueprint: Blueprint,
    handler: AgentWorkbenchHandler,
) -> None:
    """注册创建者、发布态和 Run 控制路由。"""
    blueprint.add_url_rule(
        "/agent-runs/<uuid:run_id>/events",
        methods=["GET"], view_func=handler.run_events,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/conversations/<uuid:conversation_id>/model-settings",
        methods=["GET"], view_func=handler.debug_model_settings,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/conversations/<uuid:conversation_id>/model-settings",
        methods=["GET"], view_func=handler.web_app_model_settings,
    )
    blueprint.add_url_rule(
        "/tasks/apps/<uuid:app_id>/conversations/<uuid:conversation_id>/model-settings",
        methods=["GET"], view_func=handler.creator_task_model_settings,
    )
    blueprint.add_url_rule(
        "/agent-runs/<uuid:run_id>/observability",
        methods=["GET"],
        view_func=handler.run_observability,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/conversations/<uuid:conversation_id>/commands",
        methods=["POST"],
        view_func=handler.debug_command,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/conversations/<uuid:conversation_id>/stream",
        methods=["POST"],
        view_func=handler.debug_stream,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/conversations/<uuid:conversation_id>/state",
        methods=["GET"],
        view_func=handler.debug_state,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/conversations/<uuid:conversation_id>/commands",
        methods=["POST"],
        view_func=handler.web_app_command,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/conversations/<uuid:conversation_id>/stream",
        methods=["POST"],
        view_func=handler.web_app_stream,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/conversations/<uuid:conversation_id>/state",
        methods=["GET"],
        view_func=handler.web_app_state,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/conversations/<uuid:conversation_id>/runs/<uuid:run_id>/cancel",
        methods=["POST"],
        view_func=handler.debug_cancel_run,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/conversations/<uuid:conversation_id>/runs/<uuid:run_id>/cancel",
        methods=["POST"],
        view_func=handler.web_app_cancel_run,
    )
    blueprint.add_url_rule(
        "/tasks/apps/<uuid:app_id>/conversations/<uuid:conversation_id>/commands",
        methods=["POST"],
        view_func=handler.creator_task_command,
    )
    blueprint.add_url_rule(
        "/tasks/apps/<uuid:app_id>/conversations/<uuid:conversation_id>/stream",
        methods=["POST"],
        view_func=handler.creator_task_stream,
    )
    blueprint.add_url_rule(
        "/tasks/apps/<uuid:app_id>/conversations/<uuid:conversation_id>/state",
        methods=["GET"],
        view_func=handler.creator_task_state,
    )
    blueprint.add_url_rule(
        "/tasks/apps/<uuid:app_id>/conversations/<uuid:conversation_id>/runs/<uuid:run_id>/cancel",
        methods=["POST"],
        view_func=handler.creator_task_cancel_run,
    )


def register_assistant_agent_protocol_routes(
    blueprint: Blueprint,
    handler: AssistantAgentProtocolHandler,
) -> None:
    """注册 Creator 辅助 Agent 的 Agent Protocol 路由。"""
    blueprint.add_url_rule(
        "/assistant-agent/conversations",
        methods=["POST"],
        endpoint="assistant_agent_conversation_create",
        view_func=handler.create_conversation,
    )
    prefix = "/assistant-agent/conversations/<uuid:conversation_id>"
    blueprint.add_url_rule(
        f"{prefix}/model-settings", methods=["GET"],
        endpoint="assistant_agent_model_settings", view_func=handler.model_settings,
    )
    blueprint.add_url_rule(
        f"{prefix}/commands", methods=["POST"], view_func=handler.command
    )
    blueprint.add_url_rule(
        f"{prefix}/stream", methods=["POST"], view_func=handler.stream
    )
    blueprint.add_url_rule(f"{prefix}/state", methods=["GET"], view_func=handler.state)
    blueprint.add_url_rule(
        f"{prefix}/runs/<uuid:run_id>/cancel",
        methods=["POST"],
        endpoint="assistant_agent_protocol_cancel_run",
        view_func=handler.cancel_run,
    )


def register_openapi_agent_protocol_routes(
    blueprint: Blueprint,
    handler: OpenAPIAgentProtocolHandler,
) -> None:
    """注册服务 API 的 Agent Protocol 路由。"""
    prefix = "/openapi/apps/<uuid:app_id>/conversations/<uuid:conversation_id>"
    blueprint.add_url_rule(
        f"{prefix}/model-settings", methods=["GET"],
        endpoint="openapi_agent_model_settings", view_func=handler.model_settings,
    )
    blueprint.add_url_rule(
        "/openapi/apps/<uuid:app_id>/conversations",
        methods=["POST"],
        view_func=handler.create_conversation,
    )
    blueprint.add_url_rule(
        f"{prefix}/commands",
        methods=["POST"],
        view_func=handler.command,
    )
    blueprint.add_url_rule(
        f"{prefix}/stream",
        methods=["POST"],
        view_func=handler.stream,
    )
    blueprint.add_url_rule(
        f"{prefix}/state",
        methods=["GET"],
        view_func=handler.state,
    )
    blueprint.add_url_rule(
        f"{prefix}/runs/<uuid:run_id>/cancel",
        methods=["POST"],
        endpoint="openapi_agent_protocol_cancel_run",
        view_func=handler.cancel_run,
    )


def register_internal_agent_protocol_routes(
    blueprint: Blueprint,
    handler: InternalAgentProtocolHandler,
) -> None:
    """注册供 DeepAgents AsyncSubAgent 使用的标准 SDK 路由。"""
    prefix = "/agent-protocol"
    blueprint.add_url_rule(
        f"{prefix}/threads",
        methods=["POST"],
        view_func=handler.create_thread,
    )
    blueprint.add_url_rule(
        f"{prefix}/threads/<uuid:conversation_id>",
        methods=["GET"],
        view_func=handler.get_thread,
    )
    blueprint.add_url_rule(
        f"{prefix}/threads/<uuid:conversation_id>/runs",
        methods=["POST"],
        view_func=handler.create_run,
    )
    blueprint.add_url_rule(
        f"{prefix}/threads/<uuid:conversation_id>/runs/<uuid:run_id>",
        methods=["GET"],
        view_func=handler.get_run,
    )
    blueprint.add_url_rule(
        f"{prefix}/threads/<uuid:conversation_id>/runs/<uuid:run_id>/cancel",
        methods=["POST"],
        endpoint="internal_agent_protocol_cancel_run",
        view_func=handler.cancel_run,
    )


__all__ = [
    "register_agent_workbench_routes",
    "register_assistant_agent_protocol_routes",
    "register_internal_agent_protocol_routes",
    "register_openapi_agent_protocol_routes",
]
