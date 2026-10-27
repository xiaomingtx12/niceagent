from dataclasses import dataclass
from uuid import UUID

from injector import inject
from langchain_core.tools import BaseTool, tool
from sqlalchemy import and_, or_

from config import Config
from internal.entity.conversation_entity import InvokeFrom
from internal.exception import FailException, NotFoundException
from internal.model import Account, Conversation
from internal.schema.agent.agent_tool_schema import (
    AppWithWorkflowGeneratorToolReq,
    CreateAppToolReq,
    WorkflowGeneratorToolReq,
)
from internal.task.app_task import auto_create_app
from internal.task.workflow_task import (
    auto_generate_workflow,
    auto_generate_app_with_workflow,
)
from internal.service.dataset.embeddings_service import EmbeddingsService
from internal.service.dataset.faiss_service import FaissService
from internal.service.conversation.conversation_service import ConversationService
from pkg.sqlalchemy import SQLAlchemy


@inject
@dataclass
class AssistantAgentService:
    """管理内置助手会话与 Creator 专属工具；模型及执行由统一装配入口负责。"""

    faiss_service: FaissService
    embeddings_service: EmbeddingsService
    conversation_service: ConversationService
    db: SQLAlchemy
    config: Config

    def get_or_create_conversation(self, account: Account) -> Conversation:
        """首次使用辅助 Agent 时创建会话，之后按归属复用。"""
        app_id = self._assistant_app_id()
        with self.db.auto_commit():
            locked_account = (
                self.db.session.query(Account)
                .populate_existing()
                .with_for_update()
                .filter(Account.id == account.id)
                .one_or_none()
            )
            if locked_account is None:
                raise NotFoundException("账号不存在")
            return self.conversation_service.get_or_create_assistant_conversation(
                account_id=locked_account.id,
                app_id=app_id,
            )

    def get_owned_conversation(
        self,
        *,
        conversation_id: UUID,
        account: Account,
    ) -> Conversation:
        """校验内置助手配置及当前账号的会话归属。"""
        app_id = self._assistant_app_id()
        conversation = (
            self.db.session.query(Conversation)
            .filter(
                Conversation.id == conversation_id,
                Conversation.app_id == app_id,
                Conversation.invoke_from == InvokeFrom.ASSISTANT_AGENT,
                Conversation.is_deleted.is_(False),
                or_(
                    Conversation.owner_account_id == account.id,
                    and_(Conversation.owner_account_id.is_(None), Conversation.created_by == account.id),
                ),
            )
            .one_or_none()
        )
        if conversation is None:
            raise NotFoundException("该会话不存在或被删除，请核实后重试")
        return conversation

    def build_tools(self, account_id: UUID) -> list[BaseTool]:
        """工具绑定已授权账号，执行时无需保留 ORM 对象或访问请求上下文。"""
        # 1.检索工具绑定本次账号的向量模型，后续工具执行不再读取 Flask 上下文。
        self.embeddings_service.initialize_embeddings(account_id)
        retrieval_tool = self.faiss_service.convert_faiss_to_tool(
            self.embeddings_service.embeddings,
        )

        # 2.集中挂载平台工具；工具闭包只捕获账号 ID，不持有 ORM 对象。
        return [
            retrieval_tool,
            self.convert_create_app_to_tool(account_id),
            self.convert_workflow_generator_to_tool(account_id),
            self.convert_app_with_workflow_generator_to_tool(account_id),
        ]

    @staticmethod
    def convert_create_app_to_tool(account_id: UUID) -> BaseTool:
        """定义自动创建Agent应用LangChain工具"""

        @tool("create_app", args_schema=CreateAppToolReq)
        def create_app(name: str, description: str) -> str:
            """如果用户提出了需要创建一个Agent/应用，你可以调用此工具，参数的输入是应用的名称+描述，返回的数据是创建后的成功提示"""
            # 1.调用celery异步任务在后端创建应用
            auto_create_app.delay(name, description, str(account_id))

            # 2.返回成功提示
            return f"已调用后端异步任务创建Agent应用。\n应用名称: {name}\n应用描述: {description}"

        return create_app

    @staticmethod
    def convert_workflow_generator_to_tool(account_id: UUID) -> BaseTool:
        """将工作流生成功能转换为LangChain工具"""

        account_id_str = str(account_id)

        @tool("workflow_generator", args_schema=WorkflowGeneratorToolReq)
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

    @staticmethod
    def convert_app_with_workflow_generator_to_tool(account_id: UUID) -> BaseTool:
        """将Agent应用+工作流生成功能转换为LangChain工具"""

        account_id_str = str(account_id)

        @tool(
            "app_with_workflow_generator",
            args_schema=AppWithWorkflowGeneratorToolReq,
        )
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

    def _assistant_app_id(self) -> UUID:
        """解析内置助手应用 ID；配置缺失时直接拒绝，不触碰数据库。"""
        try:
            return UUID(str(self.config.ASSISTANT_AGENT_ID))
        except (AttributeError, TypeError, ValueError) as exc:
            raise FailException("辅助 Agent 应用配置无效") from exc
