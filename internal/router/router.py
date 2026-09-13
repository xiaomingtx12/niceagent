from dataclasses import dataclass

from flask import Flask, Blueprint
from injector import inject

from internal.handler import (
    AppHandler,
    ApiToolHandler,
    DatasetHandler,
    UploadFileHandler,
    DocumentHandler,
    SegmentHandler,
    OAuthHandler,
    AccountHandler,
    AuthHandler,
    AIHandler,
    ApiKeyHandler,
    BuiltinToolHandler,
    BuiltinAppHandler,
    BuiltinWorkflowHandler,
    WorkflowHandler,
    LanguageModelHandler,
    MCPHandler,
    AnalysisHandler,
    WebAppHandler,
    ConversationHandler,
    AudioHandler,
    PlatformHandler,
    WechatHandler,
    ProviderApiKeyHandler,
    SkillHandler,
    WorkspaceFileHandler,
    MemoryFileHandler,
    DebugAgentHandler,
    WebAppAgentHandler,
    CreatorTaskAgentHandler,
    AgentRunHandler,
    AssistantAgentProtocolHandler,
    InternalAgentProtocolHandler,
    OpenAPIAgentProtocolHandler,
    CreatorTaskHandler,
    AgentEvaluationHandler,
)
from .agent_workbench_routes import (
    register_debug_agent_routes,
    register_web_app_agent_routes,
    register_creator_task_agent_routes,
    register_agent_run_routes,
    register_assistant_agent_protocol_routes,
    register_internal_agent_protocol_routes,
    register_openapi_agent_protocol_routes,
)
from .web_app_routes import register_web_app_routes
from .app_routes import register_app_routes
from .deepagents_routes import register_deepagents_routes


@inject
@dataclass
class Router:
    """路由"""

    app_handler: AppHandler
    builtin_tool_handler: BuiltinToolHandler
    api_tool_handler: ApiToolHandler
    dataset_handler: DatasetHandler
    upload_file_handler: UploadFileHandler
    document_handler: DocumentHandler
    segment_handler: SegmentHandler
    oauth_handler: OAuthHandler
    account_handler: AccountHandler
    auth_handler: AuthHandler
    ai_handler: AIHandler
    api_key_handler: ApiKeyHandler
    builtin_app_handler: BuiltinAppHandler
    builtin_workflow_handler: BuiltinWorkflowHandler
    workflow_handler: WorkflowHandler
    language_model_handler: LanguageModelHandler
    mcp_handler: MCPHandler
    analysis_handler: AnalysisHandler
    web_app_handler: WebAppHandler
    conversation_handler: ConversationHandler
    audio_handler: AudioHandler
    platform_handler: PlatformHandler
    wechat_handler: WechatHandler
    provider_api_key_handler: ProviderApiKeyHandler
    skill_handler: SkillHandler
    workspace_file_handler: WorkspaceFileHandler
    memory_file_handler: MemoryFileHandler
    debug_agent_handler: DebugAgentHandler
    web_app_agent_handler: WebAppAgentHandler
    creator_task_agent_handler: CreatorTaskAgentHandler
    agent_run_handler: AgentRunHandler
    assistant_agent_protocol_handler: AssistantAgentProtocolHandler
    internal_agent_protocol_handler: InternalAgentProtocolHandler
    openapi_agent_protocol_handler: OpenAPIAgentProtocolHandler
    creator_task_handler: CreatorTaskHandler
    agent_evaluation_handler: AgentEvaluationHandler

    def register_router(self, app: Flask):
        """注册路由"""
        # region 蓝图初始化
        # 1.创建一个蓝图（一组路由的集合/前台路由/后台路由）
        bp = Blueprint("my_dify", __name__, url_prefix="")
        openapi_bp = Blueprint("openapi", __name__, url_prefix="")

        # 2.将url与对应的控制器方法做绑定
        register_app_routes(bp, self.app_handler)
        evaluation = self.agent_evaluation_handler
        bp.add_url_rule("/apps/<uuid:app_id>/evaluation-sources", view_func=evaluation.evaluation_sources)
        bp.add_url_rule("/apps/<uuid:app_id>/evaluation-sources/<uuid:run_id>", view_func=evaluation.evaluation_source)
        bp.add_url_rule("/apps/<uuid:app_id>/evaluations", methods=["POST"], view_func=evaluation.create_evaluation)
        bp.add_url_rule("/apps/<uuid:app_id>/evaluations", methods=["GET"], view_func=evaluation.evaluation_history)
        bp.add_url_rule("/apps/<uuid:app_id>/evaluations/<uuid:evaluation_id>", view_func=evaluation.evaluation_detail)
        # endregion

        # region 内置插件广场
        # 3.内置插件广场模块
        bp.add_url_rule(
            "/builtin-tools", view_func=self.builtin_tool_handler.get_builtin_tools
        )
        bp.add_url_rule(
            "/builtin-tools/<string:provider_name>/tools/<string:tool_name>",
            view_func=self.builtin_tool_handler.get_provider_tool,
        )
        bp.add_url_rule(
            "/builtin-tools/<string:provider_name>/icon",
            view_func=self.builtin_tool_handler.get_provider_icon,
        )
        bp.add_url_rule(
            "/builtin-tools/categories",
            view_func=self.builtin_tool_handler.get_categories,
        )

        # endregion

        # region 自定义 API 插件
        # 4.自定义API插件模块
        bp.add_url_rule(
            "/api-tools",
            view_func=self.api_tool_handler.get_api_tool_providers_with_page,
        )
        bp.add_url_rule(
            "/api-tools/validate-openapi-schema",
            methods=["POST"],
            view_func=self.api_tool_handler.validate_openapi_schema,
        )
        bp.add_url_rule(
            "/api-tools",
            methods=["POST"],
            view_func=self.api_tool_handler.create_api_tool_provider,
        )
        bp.add_url_rule(
            "/api-tools/<uuid:provider_id>",
            view_func=self.api_tool_handler.get_api_tool_provider,
        )
        bp.add_url_rule(
            "/api-tools/<uuid:provider_id>",
            methods=["POST"],
            view_func=self.api_tool_handler.update_api_tool_provider,
        )
        bp.add_url_rule(
            "/api-tools/<uuid:provider_id>/tools/<string:tool_name>",
            view_func=self.api_tool_handler.get_api_tool,
        )
        bp.add_url_rule(
            "/api-tools/<uuid:provider_id>/tools/<string:tool_name>/debug",
            methods=["POST"],
            view_func=self.api_tool_handler.debug_api_tool,
        )
        bp.add_url_rule(
            "/api-tools/<uuid:provider_id>/delete",
            methods=["POST"],
            view_func=self.api_tool_handler.delete_api_tool_provider,
        )
        bp.add_url_rule(
            "/mcp-servers",
            view_func=self.mcp_handler.get_mcp_servers_with_page,
        )
        bp.add_url_rule(
            "/mcp-servers",
            methods=["POST"],
            view_func=self.mcp_handler.create_mcp_server,
        )
        bp.add_url_rule(
            "/mcp-servers/<uuid:provider_id>",
            view_func=self.mcp_handler.get_mcp_server,
        )
        bp.add_url_rule(
            "/mcp-servers/<uuid:provider_id>",
            methods=["POST"],
            view_func=self.mcp_handler.update_mcp_server,
        )
        bp.add_url_rule(
            "/mcp-servers/<uuid:provider_id>/refresh",
            methods=["POST"],
            view_func=self.mcp_handler.refresh_mcp_server,
        )
        bp.add_url_rule(
            "/mcp-servers/<uuid:provider_id>/tools/<string:tool_name>",
            view_func=self.mcp_handler.get_mcp_tool,
        )
        bp.add_url_rule(
            "/mcp-servers/<uuid:provider_id>/tools/<string:tool_name>/debug",
            methods=["POST"],
            view_func=self.mcp_handler.debug_mcp_tool,
        )
        bp.add_url_rule(
            "/mcp-servers/<uuid:provider_id>/delete",
            methods=["POST"],
            view_func=self.mcp_handler.delete_mcp_server,
        )
        # endregion

        # region 上传文件
        # 5.上传文件模块
        bp.add_url_rule(
            "/upload-files/file",
            methods=["POST"],
            view_func=self.upload_file_handler.upload_file,
        )
        bp.add_url_rule(
            "/upload-files/image",
            methods=["POST"],
            view_func=self.upload_file_handler.upload_image,
        )

        # endregion

        # region 知识库
        # 6.知识库模块
        bp.add_url_rule(
            "/datasets", view_func=self.dataset_handler.get_datasets_with_page
        )
        bp.add_url_rule(
            "/datasets", methods=["POST"], view_func=self.dataset_handler.create_dataset
        )
        bp.add_url_rule(
            "/datasets/<uuid:dataset_id>", view_func=self.dataset_handler.get_dataset
        )
        bp.add_url_rule(
            "/datasets/<uuid:dataset_id>",
            methods=["POST"],
            view_func=self.dataset_handler.update_dataset,
        )
        bp.add_url_rule(
            "/datasets/<uuid:dataset_id>/queries",
            view_func=self.dataset_handler.get_dataset_queries,
        )
        bp.add_url_rule(
            "/datasets/<uuid:dataset_id>/delete",
            methods=["POST"],
            view_func=self.dataset_handler.delete_dataset,
        )
        bp.add_url_rule(
            "/datasets/<uuid:dataset_id>/documents",
            view_func=self.document_handler.get_documents_with_page,
        )
        bp.add_url_rule(
            "/datasets/<uuid:dataset_id>/documents",
            methods=["POST"],
            view_func=self.document_handler.create_documents,
        )
        bp.add_url_rule(
            "/datasets/<uuid:dataset_id>/documents/<uuid:document_id>",
            view_func=self.document_handler.get_document,
        )
        bp.add_url_rule(
            "/datasets/<uuid:dataset_id>/documents/<uuid:document_id>/name",
            methods=["POST"],
            view_func=self.document_handler.update_document_name,
        )
        bp.add_url_rule(
            "/datasets/<uuid:dataset_id>/documents/<uuid:document_id>/enabled",
            methods=["POST"],
            view_func=self.document_handler.update_document_enabled,
        )
        bp.add_url_rule(
            "/datasets/<uuid:dataset_id>/documents/<uuid:document_id>/delete",
            methods=["POST"],
            view_func=self.document_handler.delete_document,
        )
        bp.add_url_rule(
            "/datasets/<uuid:dataset_id>/documents/batch/<string:batch>",
            view_func=self.document_handler.get_documents_status,
        )
        bp.add_url_rule(
            "/datasets/<uuid:dataset_id>/documents/<uuid:document_id>/segments",
            view_func=self.segment_handler.get_segments_with_page,
        )
        bp.add_url_rule(
            "/datasets/<uuid:dataset_id>/documents/<uuid:document_id>/segments",
            methods=["POST"],
            view_func=self.segment_handler.create_segment,
        )
        bp.add_url_rule(
            "/datasets/<uuid:dataset_id>/documents/<uuid:document_id>/segments/<uuid:segment_id>",
            view_func=self.segment_handler.get_segment,
        )
        bp.add_url_rule(
            "/datasets/<uuid:dataset_id>/documents/<uuid:document_id>/segments/<uuid:segment_id>",
            methods=["POST"],
            view_func=self.segment_handler.update_segment,
        )
        bp.add_url_rule(
            "/datasets/<uuid:dataset_id>/documents/<uuid:document_id>/segments/<uuid:segment_id>/enabled",
            methods=["POST"],
            view_func=self.segment_handler.update_segment_enabled,
        )
        bp.add_url_rule(
            "/datasets/<uuid:dataset_id>/documents/<uuid:document_id>/segments/<uuid:segment_id>/delete",
            methods=["POST"],
            view_func=self.segment_handler.delete_segment,
        )
        bp.add_url_rule(
            "/datasets/<uuid:dataset_id>/hit",
            methods=["POST"],
            view_func=self.dataset_handler.hit,
        )

        # endregion

        # region 授权认证
        # 7.授权认证模块
        bp.add_url_rule(
            "/oauth/<string:provider_name>",
            view_func=self.oauth_handler.provider,
        )
        bp.add_url_rule(
            "/oauth/authorize/<string:provider_name>",
            methods=["POST"],
            view_func=self.oauth_handler.authorize,
        )
        bp.add_url_rule(
            "/auth/password-login",
            methods=["POST"],
            view_func=self.auth_handler.password_login,
        )
        bp.add_url_rule(
            "/auth/logout",
            methods=["POST"],
            view_func=self.auth_handler.logout,
        )

        # endregion

        # region 账号设置
        # 8.账号设置模块
        bp.add_url_rule("/account", view_func=self.account_handler.get_current_user)
        bp.add_url_rule(
            "/account/password",
            methods=["POST"],
            view_func=self.account_handler.update_password,
        )
        bp.add_url_rule(
            "/account/name",
            methods=["POST"],
            view_func=self.account_handler.update_name,
        )
        bp.add_url_rule(
            "/account/avatar",
            methods=["POST"],
            view_func=self.account_handler.update_avatar,
        )
        # endregion

        # region AI 辅助
        # 8.AI辅助模块
        bp.add_url_rule(
            "/ai/optimize-prompt",
            methods=["POST"],
            view_func=self.ai_handler.optimize_prompt,
        )
        bp.add_url_rule(
            "/ai/suggested-questions",
            methods=["POST"],
            view_func=self.ai_handler.generate_suggested_questions,
        )
        # endregion

        # region API 密钥
        # 9.API秘钥模块
        bp.add_url_rule(
            "/openapi/api-keys", view_func=self.api_key_handler.get_api_keys_with_page
        )
        bp.add_url_rule(
            "/openapi/api-keys",
            methods=["POST"],
            view_func=self.api_key_handler.create_api_key,
        )
        bp.add_url_rule(
            "/openapi/api-keys/<uuid:api_key_id>",
            methods=["POST"],
            view_func=self.api_key_handler.update_api_key,
        )
        bp.add_url_rule(
            "/openapi/api-keys/<uuid:api_key_id>/is-active",
            methods=["POST"],
            view_func=self.api_key_handler.update_api_key_is_active,
        )
        bp.add_url_rule(
            "/openapi/api-keys/<uuid:api_key_id>/delete",
            methods=["POST"],
            view_func=self.api_key_handler.delete_api_key,
        )

        # endregion

        # region 提供商 API 密钥
        # 9.1 提供商API密钥模块
        bp.add_url_rule(
            "/provider-api-keys",
            view_func=self.provider_api_key_handler.get_provider_api_keys_with_page,
        )
        bp.add_url_rule(
            "/provider-api-keys",
            methods=["POST"],
            view_func=self.provider_api_key_handler.create_provider_api_key,
        )
        bp.add_url_rule(
            "/provider-api-keys/<uuid:provider_api_key_id>",
            view_func=self.provider_api_key_handler.get_provider_api_key,
        )
        bp.add_url_rule(
            "/provider-api-keys/<uuid:provider_api_key_id>",
            methods=["POST"],
            view_func=self.provider_api_key_handler.update_provider_api_key,
        )
        bp.add_url_rule(
            "/provider-api-keys/<uuid:provider_api_key_id>/models",
            methods=["POST"],
            view_func=self.provider_api_key_handler.update_provider_api_key_models,
        )
        bp.add_url_rule(
            "/provider-api-keys/<uuid:provider_api_key_id>/delete",
            methods=["POST"],
            view_func=self.provider_api_key_handler.delete_provider_api_key,
        )
        register_openapi_agent_protocol_routes(
            openapi_bp,
            self.openapi_agent_protocol_handler,
        )
        # endregion

        # region 内置应用
        # 10.内置应用模块
        bp.add_url_rule(
            "/builtin-apps/categories",
            view_func=self.builtin_app_handler.get_builtin_app_categories,
        )
        bp.add_url_rule(
            "/builtin-apps", view_func=self.builtin_app_handler.get_builtin_apps
        )
        bp.add_url_rule(
            "/builtin-apps/add-builtin-app-to-space",
            methods=["POST"],
            view_func=self.builtin_app_handler.add_builtin_app_to_space,
        )

        # endregion

        # region 内置工作流模板
        # 10.5.内置工作流模板模块
        bp.add_url_rule(
            "/builtin-workflows/categories",
            view_func=self.builtin_workflow_handler.get_builtin_workflow_categories,
        )
        bp.add_url_rule(
            "/builtin-workflows",
            view_func=self.builtin_workflow_handler.get_builtin_workflows,
        )
        bp.add_url_rule(
            "/builtin-workflows/<string:builtin_workflow_id>",
            view_func=self.builtin_workflow_handler.get_builtin_workflow_detail,
        )
        bp.add_url_rule(
            "/builtin-workflows/<string:builtin_workflow_id>/create",
            methods=["POST"],
            view_func=self.builtin_workflow_handler.create_workflow_from_template,
        )

        # endregion

        # region 工作流
        # 11.工作流模块
        bp.add_url_rule(
            "/workflows", view_func=self.workflow_handler.get_workflows_with_page
        )
        bp.add_url_rule(
            "/workflows",
            methods=["POST"],
            view_func=self.workflow_handler.create_workflow,
        )
        bp.add_url_rule(
            "/workflows/<uuid:workflow_id>",
            view_func=self.workflow_handler.get_workflow,
        )
        bp.add_url_rule(
            "/workflows/<uuid:workflow_id>",
            methods=["POST"],
            view_func=self.workflow_handler.update_workflow,
        )
        bp.add_url_rule(
            "/workflows/<uuid:workflow_id>/delete",
            methods=["POST"],
            view_func=self.workflow_handler.delete_workflow,
        )
        bp.add_url_rule(
            "/workflows/<uuid:workflow_id>/draft-graph",
            methods=["POST"],
            view_func=self.workflow_handler.update_draft_graph,
        )
        bp.add_url_rule(
            "/workflows/<uuid:workflow_id>/draft-graph",
            view_func=self.workflow_handler.get_draft_graph,
        )
        bp.add_url_rule(
            "/workflows/<uuid:workflow_id>/debug",
            methods=["POST"],
            view_func=self.workflow_handler.debug_workflow,
        )
        bp.add_url_rule(
            "/workflows/<uuid:workflow_id>/publish",
            methods=["POST"],
            view_func=self.workflow_handler.publish_workflow,
        )
        bp.add_url_rule(
            "/workflows/<uuid:workflow_id>/cancel-publish",
            methods=["POST"],
            view_func=self.workflow_handler.cancel_publish_workflow,
        )

        # endregion

        # region 语言模型
        # 12.语言模型模块
        bp.add_url_rule(
            "/language-models",
            view_func=self.language_model_handler.get_language_models,
        )
        bp.add_url_rule(
            "/language-models/discover",
            methods=["POST"],
            view_func=self.language_model_handler.discover_language_models,
        )
        bp.add_url_rule(
            "/language-models/<string:provider_name>/icon",
            view_func=self.language_model_handler.get_language_model_icon,
        )
        bp.add_url_rule(
            "/language-models/<string:provider_name>/<path:model_name>",
            view_func=self.language_model_handler.get_language_model,
        )

        # endregion

        # region 应用统计
        # 14.应用统计模块
        bp.add_url_rule(
            "/analysis/<uuid:app_id>",
            view_func=self.analysis_handler.get_app_analysis,
        )

        # endregion

        # region WebApp
        # 15.WebApp模块
        register_web_app_routes(bp, self.web_app_handler)

        # endregion

        # region Agent 工作台
        register_debug_agent_routes(bp, self.debug_agent_handler)
        register_web_app_agent_routes(bp, self.web_app_agent_handler)
        register_creator_task_agent_routes(bp, self.creator_task_agent_handler)
        register_agent_run_routes(bp, self.agent_run_handler)
        register_assistant_agent_protocol_routes(
            bp,
            self.assistant_agent_protocol_handler,
        )
        register_internal_agent_protocol_routes(
            bp,
            self.internal_agent_protocol_handler,
        )
        # Creator 任务中心的 Agent 列表与统一会话历史。
        bp.add_url_rule(
            "/tasks/agents", endpoint="creator_task_agents", view_func=self.creator_task_handler.get_agents
        )
        bp.add_url_rule(
            "/tasks/agents/<uuid:app_id>",
            endpoint="creator_task_agent",
            view_func=self.creator_task_handler.get_agent,
        )
        bp.add_url_rule(
            "/tasks/conversations",
            endpoint="creator_task_conversations",
            view_func=self.creator_task_handler.get_conversations,
        )
        bp.add_url_rule(
            "/tasks/apps/<uuid:app_id>/conversations",
            methods=["POST"],
            endpoint="creator_task_conversation_create",
            view_func=self.creator_task_handler.create_conversation,
        )
        bp.add_url_rule(
            "/tasks/conversations/<uuid:conversation_id>",
            endpoint="creator_task_conversation",
            view_func=self.creator_task_handler.get_conversation,
        )
        bp.add_url_rule(
            "/tasks/conversations/<uuid:conversation_id>/name",
            methods=["PATCH"],
            endpoint="creator_task_conversation_name",
            view_func=self.creator_task_handler.update_conversation_name,
        )
        bp.add_url_rule(
            "/tasks/conversations/<uuid:conversation_id>/is-pinned",
            methods=["PATCH"],
            endpoint="creator_task_conversation_pin",
            view_func=self.creator_task_handler.update_conversation_is_pinned,
        )
        bp.add_url_rule(
            "/tasks/conversations/<uuid:conversation_id>",
            methods=["DELETE"],
            endpoint="creator_task_conversation_delete",
            view_func=self.creator_task_handler.delete_conversation,
        )
        # endregion

        # region 会话
        # 16.会话模块
        bp.add_url_rule(
            "/conversations/<uuid:conversation_id>/messages",
            view_func=self.conversation_handler.get_messages,
        )
        bp.add_url_rule(
            "/conversations/<uuid:conversation_id>/delete",
            methods=["POST"],
            view_func=self.conversation_handler.delete_conversation,
        )
        bp.add_url_rule(
            "/conversations/<uuid:conversation_id>/messages/<uuid:message_id>/delete",
            methods=["POST"],
            view_func=self.conversation_handler.delete_message,
        )
        bp.add_url_rule(
            "/conversations/<uuid:conversation_id>/name",
            view_func=self.conversation_handler.get_conversation_name,
        )
        bp.add_url_rule(
            "/conversations/<uuid:conversation_id>/name",
            methods=["POST"],
            view_func=self.conversation_handler.update_conversation_name,
        )
        bp.add_url_rule(
            "/conversations/<uuid:conversation_id>/is-pinned",
            methods=["POST"],
            view_func=self.conversation_handler.update_conversation_is_pinned,
        )

        # endregion

        # region 语音转换
        # 17.语音转换模块
        bp.add_url_rule(
            "/audio/audio-to-text",
            methods=["POST"],
            view_func=self.audio_handler.audio_to_text,
        )
        bp.add_url_rule(
            "/audio/message-to-audio",
            methods=["POST"],
            view_func=self.audio_handler.message_to_audio,
        )

        # endregion

        # region 第三方平台配置
        # 18.第三方平台配置模块
        bp.add_url_rule(
            "/platform/<uuid:app_id>/wechat-config",
            view_func=self.platform_handler.get_wechat_config,
        )
        bp.add_url_rule(
            "/platform/<uuid:app_id>/wechat-config",
            methods=["POST"],
            view_func=self.platform_handler.update_wechat_config,
        )
        bp.add_url_rule(
            "/wechat/<uuid:app_id>",
            methods=["GET", "POST"],
            view_func=self.wechat_handler.wechat,
        )

        # endregion

        # region DeepAgents 平台资源
        register_deepagents_routes(
            bp,
            skill_handler=self.skill_handler,
            workspace_file_handler=self.workspace_file_handler,
            memory_file_handler=self.memory_file_handler,
        )
        # endregion

        # region 注册蓝图
        # 20.在应用上去注册蓝图
        app.register_blueprint(bp)
        app.register_blueprint(openapi_bp)
        # endregion
