"""Agent 工作台路由注册。"""

from flask import Blueprint

from internal.handler import (
    AgentWorkbenchHandler,
    AssistantAgentProtocolHandler,
    OpenAPIAgentProtocolHandler,
)


def register_agent_workbench_routes(
    blueprint: Blueprint,
    handler: AgentWorkbenchHandler,
) -> None:
    """注册创建者、发布态和 Run 控制路由。"""
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/threads/<uuid:thread_id>/commands",
        methods=["POST"],
        view_func=handler.debug_command,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/threads/<uuid:thread_id>/stream",
        methods=["POST"],
        view_func=handler.debug_stream,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/threads/<uuid:thread_id>/state",
        methods=["GET"],
        view_func=handler.debug_state,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/threads/<uuid:thread_id>/messages",
        methods=["GET"],
        view_func=handler.debug_messages,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/threads/<uuid:thread_id>/commands",
        methods=["POST"],
        view_func=handler.web_app_command,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/threads/<uuid:thread_id>/stream",
        methods=["POST"],
        view_func=handler.web_app_stream,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/threads/<uuid:thread_id>/state",
        methods=["GET"],
        view_func=handler.web_app_state,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/threads/<uuid:thread_id>/messages",
        methods=["GET"],
        view_func=handler.web_app_messages,
    )
    blueprint.add_url_rule(
        "/apps/<uuid:app_id>/threads/<uuid:thread_id>/runs/<uuid:run_id>/cancel",
        methods=["POST"],
        view_func=handler.debug_cancel_run,
    )
    blueprint.add_url_rule(
        "/web-apps/<string:token>/threads/<uuid:thread_id>/runs/<uuid:run_id>/cancel",
        methods=["POST"],
        view_func=handler.web_app_cancel_run,
    )


def register_assistant_agent_protocol_routes(
    blueprint: Blueprint,
    handler: AssistantAgentProtocolHandler,
) -> None:
    """注册 Creator 辅助 Agent 的 Agent Protocol 路由。"""
    prefix = "/assistant-agent/threads/<uuid:thread_id>"
    blueprint.add_url_rule(
        f"{prefix}/commands", methods=["POST"], view_func=handler.command
    )
    blueprint.add_url_rule(
        f"{prefix}/stream", methods=["POST"], view_func=handler.stream
    )
    blueprint.add_url_rule(f"{prefix}/state", methods=["GET"], view_func=handler.state)
    blueprint.add_url_rule(
        f"{prefix}/messages", methods=["GET"], view_func=handler.messages
    )
    blueprint.add_url_rule(
        f"{prefix}/runs/<uuid:run_id>/cancel",
        methods=["POST"],
        view_func=handler.cancel_run,
    )


def register_openapi_agent_protocol_routes(
    blueprint: Blueprint,
    handler: OpenAPIAgentProtocolHandler,
) -> None:
    """注册服务 API 的 Agent Protocol 路由。"""
    prefix = "/openapi/apps/<uuid:app_id>/threads/<uuid:thread_id>"
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
        f"{prefix}/messages",
        methods=["GET"],
        view_func=handler.messages,
    )
    blueprint.add_url_rule(
        f"{prefix}/runs/<uuid:run_id>/cancel",
        methods=["POST"],
        view_func=handler.cancel_run,
    )


__all__ = [
    "register_agent_workbench_routes",
    "register_assistant_agent_protocol_routes",
    "register_openapi_agent_protocol_routes",
]
