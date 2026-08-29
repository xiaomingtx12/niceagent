from dataclasses import dataclass
from typing import Any
from uuid import UUID

from injector import inject
from langchain_core.tools import BaseTool, tool
from pydantic import BaseModel, Field

from internal.model import Account
from internal.task.app_task import auto_create_app
from internal.task.workflow_task import (
    auto_generate_workflow,
    auto_generate_app_with_workflow,
)
from pkg.sqlalchemy import SQLAlchemy
from .base_service import BaseService
from .embeddings_service import EmbeddingsService
from .faiss_service import FaissService
from .language_model_service import LanguageModelService


@inject
@dataclass
class AssistantAgentService(BaseService):
    """装配 Creator 辅助 Agent 的专属模型和平台工具。

    创建 App、生成 Workflow 等能力属于 Creator 业务；本服务只提供模型
    和工具，实际 Run 生命周期及 Agent 执行仍复用工作台和 Agent Core。
    """

    db: SQLAlchemy
    faiss_service: FaissService
    embeddings_service: EmbeddingsService
    language_model_service: LanguageModelService

    def prepare_runtime(self, account: Account) -> tuple[Any, list[BaseTool]]:
        """构造辅助 Agent 专属模型与工具，执行仍交给 Agent Protocol Run。"""
        from internal.exception import FailException

        try:
            self.embeddings_service.initialize_embeddings(account.id)
            self.faiss_service.initialize_faiss()
        except Exception as exc:
            raise FailException(f"辅助智能体会话依赖初始化失败: {exc}") from exc

        # 使用当前账号已激活模型作为辅助 Agent 的模型。
        llm = self.language_model_service.load_language_model(
            self.language_model_service.get_default_model_config(account.id),
            account.id,
        )

        # 辅助 Agent 的平台工具在这里集中装配。
        tools = [
            self.faiss_service.convert_faiss_to_tool(),
            self.convert_create_app_to_tool(account.id),
            self.convert_workflow_generator_to_tool(account),
            self.convert_app_with_workflow_generator_to_tool(account),
        ]

        return llm, tools

    @classmethod
    def convert_create_app_to_tool(cls, account_id: UUID) -> BaseTool:
        """定义自动创建Agent应用LangChain工具"""

        class CreateAppInput(BaseModel):
            """创建Agent/应用输入结构"""

            name: str = Field(
                description="需要创建的Agent/应用名称，长度不超过50个字符"
            )
            description: str = Field(
                description="需要创建的Agent/应用描述，请详细概括该应用的功能"
            )

        @tool("create_app", args_schema=CreateAppInput)
        def create_app(name: str, description: str) -> str:
            """如果用户提出了需要创建一个Agent/应用，你可以调用此工具，参数的输入是应用的名称+描述，返回的数据是创建后的成功提示"""
            # 1.调用celery异步任务在后端创建应用
            auto_create_app.delay(name, description, str(account_id))

            # 2.返回成功提示
            return f"已调用后端异步任务创建Agent应用。\n应用名称: {name}\n应用描述: {description}"

        return create_app

    @classmethod
    def convert_workflow_generator_to_tool(cls, account: Account) -> BaseTool:
        """将工作流生成功能转换为LangChain工具"""

        # 提前获取account_id字符串，避免在闭包中访问account对象
        account_id_str = str(account.id)

        class WorkflowGeneratorInput(BaseModel):
            """工作流生成输入结构"""

            name: str = Field(description="工作流名称，简洁明了，不超过50个字符")
            description: str = Field(description="工作流描述，概括工作流的功能")
            requirements: str = Field(
                description="详细的工作流需求说明，包括需要的功能、处理流程、节点类型等"
            )
            node_types: str = Field(
                default="",
                description="可选，指定需要使用的节点类型，多个类型用逗号分隔，如：start,llm,template_transform,end。留空则自动选择",
            )

        @tool("workflow_generator", args_schema=WorkflowGeneratorInput)
        def workflow_generator(
            name: str, description: str, requirements: str, node_types: str = ""
        ) -> str:
            """当用户需要创建工作流时调用此工具。根据用户需求自动生成工作流配置，包括节点和边的定义。返回创建成功的工作流信息。"""
            # 1. 解析节点类型
            node_types_list = None
            if node_types and node_types.strip():
                node_types_list = [nt.strip() for nt in node_types.split(",")]

            # 2. 调用Celery异步任务在后端生成工作流（使用预先获取的account_id字符串）
            auto_generate_workflow.delay(
                name, description, requirements, account_id_str, node_types_list
            )

            # 3. 返回成功提示
            return f"""已调用后端异步任务生成工作流。

工作流信息：
- 名称：{name}
- 描述：{description}
- 需求：{requirements}

工作流正在后台生成中，预计需要10-30秒。
生成完成后，您可以在工作流管理页面查看和编辑此工作流。"""

        return workflow_generator

    @classmethod
    def convert_app_with_workflow_generator_to_tool(cls, account: Account) -> BaseTool:
        """将Agent应用+工作流生成功能转换为LangChain工具"""

        # 提前获取account_id字符串，避免在闭包中访问account对象
        account_id_str = str(account.id)

        class AppWithWorkflowGeneratorInput(BaseModel):
            """Agent应用+工作流生成输入结构"""

            app_name: str = Field(description="Agent应用名称，简洁明了，不超过50个字符")
            app_description: str = Field(description="Agent应用描述，概括应用的功能")
            preset_prompt: str = Field(
                description="Agent的预设提示词，定义Agent的人设、角色和回复逻辑"
            )
            workflow_requirements: str = Field(
                description="需要生成的工作流的详细需求说明，包括功能、处理流程等"
            )
            auto_publish: bool = Field(
                default=False,
                description="是否自动发布应用和工作流，默认为False（保存为草稿）",
            )

        @tool("app_with_workflow_generator", args_schema=AppWithWorkflowGeneratorInput)
        def app_with_workflow_generator(
            app_name: str,
            app_description: str,
            preset_prompt: str,
            workflow_requirements: str,
            auto_publish: bool = False,
        ) -> str:
            """当用户需要创建一个完整的Agent应用并配套工作流时调用此工具。一键创建Agent应用并自动生成关联的工作流。返回创建成功的应用和工作流信息。"""
            # 1. 调用Celery异步任务在后端创建应用和工作流（使用预先获取的account_id字符串）
            auto_generate_app_with_workflow.delay(
                app_name,
                app_description,
                preset_prompt,
                workflow_requirements,
                account_id_str,
                auto_publish,
            )

            # 2. 返回成功提示
            publish_status = "并自动发布" if auto_publish else "（保存为草稿）"
            return f"""已调用后端异步任务创建Agent应用和工作流。

应用信息：
- 应用名称：{app_name}
- 应用描述：{app_description}
- 预设提示词：{preset_prompt[:100]}...

工作流信息：
- 工作流名称：{app_name}_workflow
- 工作流需求：{workflow_requirements}

应用和工作流正在后台生成中，预计需要20-40秒。
生成完成后{publish_status}，您可以在应用管理页面和工作流管理页面查看。"""

        return app_with_workflow_generator
