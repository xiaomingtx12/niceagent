"""按业务入口注册 Agent 工作台与 SDK 路由。"""

from flask import Blueprint

from internal.handler import (
    AgentExecutionHandler,
    DebugAgentHandler,
    WebAppAgentHandler,
    CreatorTaskAgentHandler,
    AssistantAgentProtocolHandler,
    InternalAgentProtocolHandler,
    OpenAPIAgentProtocolHandler,
)


def register_debug_agent_routes(blueprint: Blueprint, handler: DebugAgentHandler) -> None:
    """注册应用调试入口，保留原 URL 和 Flask endpoint。"""
    prefix = "/apps/<uuid:app_id>/conversations/<uuid:conversation_id>"
    blueprint.add_url_rule(
        f"{prefix}/commands", methods=["POST"],
        endpoint="debug_command", view_func=handler.command,
    )
    blueprint.add_url_rule(
        f"{prefix}/stream", methods=["POST"],
        endpoint="debug_stream", view_func=handler.stream,
    )
    blueprint.add_url_rule(
        f"{prefix}/state", methods=["GET"],
        endpoint="debug_state", view_func=handler.state,
    )
    blueprint.add_url_rule(
        f"{prefix}/model-settings", methods=["GET"],
        endpoint="debug_model_settings", view_func=handler.model_settings,
    )
    blueprint.add_url_rule(
        f"{prefix}/runs/<uuid:run_id>/cancel", methods=["POST"],
        endpoint="debug_cancel_run", view_func=handler.cancel_run,
    )


def register_web_app_agent_routes(blueprint: Blueprint, handler: WebAppAgentHandler) -> None:
    """注册发布态 WebApp 入口，保留原 URL 和 Flask endpoint。"""
    prefix = "/web-apps/<string:token>/conversations/<uuid:conversation_id>"
    blueprint.add_url_rule(
        f"{prefix}/commands", methods=["POST"],
        endpoint="web_app_command", view_func=handler.command,
    )
    blueprint.add_url_rule(
        f"{prefix}/stream", methods=["POST"],
        endpoint="web_app_stream", view_func=handler.stream,
    )
    blueprint.add_url_rule(
        f"{prefix}/state", methods=["GET"],
        endpoint="web_app_state", view_func=handler.state,
    )
    blueprint.add_url_rule(
        f"{prefix}/model-settings", methods=["GET"],
        endpoint="web_app_model_settings", view_func=handler.model_settings,
    )
    blueprint.add_url_rule(
        f"{prefix}/runs/<uuid:run_id>/cancel", methods=["POST"],
        endpoint="web_app_cancel_run", view_func=handler.cancel_run,
    )


def register_creator_task_agent_routes(blueprint: Blueprint, handler: CreatorTaskAgentHandler) -> None:
    """注册 Creator 任务中心入口，保留原 URL 和 Flask endpoint。"""
    prefix = "/tasks/apps/<uuid:app_id>/conversations/<uuid:conversation_id>"
    blueprint.add_url_rule(
        f"{prefix}/commands", methods=["POST"],
        endpoint="creator_task_command", view_func=handler.command,
    )
    blueprint.add_url_rule(
        f"{prefix}/stream", methods=["POST"],
        endpoint="creator_task_stream", view_func=handler.stream,
    )
    blueprint.add_url_rule(
        f"{prefix}/state", methods=["GET"],
        endpoint="creator_task_state", view_func=handler.state,
    )
    blueprint.add_url_rule(
        f"{prefix}/model-settings", methods=["GET"],
        endpoint="creator_task_model_settings", view_func=handler.model_settings,
    )
    blueprint.add_url_rule(
        f"{prefix}/runs/<uuid:run_id>/cancel", methods=["POST"],
        endpoint="creator_task_cancel_run", view_func=handler.cancel_run,
    )


def register_agent_execution_routes(blueprint: Blueprint, handler: AgentExecutionHandler) -> None:
    """Run 查询独立于入口类型，保留历史路由标识。"""
    blueprint.add_url_rule(
        "/agent-runs/<uuid:run_id>/snapshot", methods=["GET"],
        endpoint="run_snapshot", view_func=handler.snapshot,
    )
    blueprint.add_url_rule(
        "/agent-runs/<uuid:run_id>/events", methods=["GET"],
        endpoint="run_events", view_func=handler.events,
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
    "register_debug_agent_routes",
    "register_web_app_agent_routes",
    "register_creator_task_agent_routes",
    "register_agent_execution_routes",
    "register_assistant_agent_protocol_routes",
    "register_internal_agent_protocol_routes",
    "register_openapi_agent_protocol_routes",
]
