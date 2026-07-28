import json
from dataclasses import dataclass
from typing import Generator, Any
from uuid import UUID

from flask import current_app
from injector import inject
from sqlalchemy import desc

from internal.core.agent.agents import BaseAgent, AgentQueueManager
from internal.core.agent.entities.agent_entity import AgentConfig
from internal.core.agent.entities.queue_entity import QueueEvent
from internal.core.language_model import LanguageModelManager
from internal.core.language_model.entities.model_entity import ModelFeature
from internal.core.memory import load_history_messages
from internal.entity.app_entity import AppStatus
from internal.entity.conversation_entity import InvokeFrom, MessageStatus
from internal.entity.dataset_entity import RetrievalSource
from internal.exception import NotFoundException, ForbiddenException
from internal.model import App, Account, Conversation, EndUser, Message
from internal.schema.web_app_schema import WebAppChatReq
from pkg.sqlalchemy import SQLAlchemy
from .app_config_service import AppConfigService
from .agent_runtime_service import AgentRuntimeService
from .base_service import BaseService
from .conversation_service import ConversationService
from .language_model_service import LanguageModelService
from .retrieval_service import RetrievalService


@inject
@dataclass
class WebAppService(BaseService):
    """WebApp服务"""

    db: SQLAlchemy
    app_config_service: AppConfigService
    retrieval_service: RetrievalService
    conversation_service: ConversationService
    language_model_service: LanguageModelService
    language_model_manager: LanguageModelManager
    agent_runtime_service: AgentRuntimeService

    def resolve_web_app_end_user(
        self,
        *,
        app: App,
        account: Account,
    ) -> EndUser:
        """Resolve the authenticated WebApp account to an app-scoped EndUser."""
        end_user = (
            self.db.session.query(EndUser)
            .filter(
                EndUser.app_id == app.id,
                EndUser.external_user_id == account.id,
            )
            .one_or_none()
        )
        if end_user is None:
            end_user = EndUser(
                tenant_id=app.account_id,
                app_id=app.id,
                external_user_id=account.id,
            )
            self.db.session.add(end_user)
            self.db.session.commit()
        return end_user

    def get_web_app(self, token: str) -> App:
        """根据传递的token获取WebApp实例"""
        # 1.在数据库中查询token对应的应用
        app = (
            self.db.session.query(App)
            .filter(
                App.token == token,
            )
            .one_or_none()
        )
        if not app or app.status != AppStatus.PUBLISHED:
            raise NotFoundException("该WebApp不存在或者未发布，请核实后重试")

        # 2.返回查询的应用
        return app

    def get_web_app_info(self, token: str) -> dict[str, Any]:
        """根据传递的token获取WebApp信息"""
        # 1.获取App基础信息
        app = self.get_web_app(token)

        # 2.获取应用配置
        app_config = self.app_config_service.get_app_config(app)
        
        # 3.从模型配置中获取模型特性（不需要真正加载模型实例）
        model_config = app_config.get("model_config", {})
        provider_name = model_config.get("provider", "")
        model_name = model_config.get("model", "")
        
        # 4.从模型管理器获取模型实体信息（不需要 API Key）
        features = []
        try:
            provider = self.language_model_manager.get_provider(provider_name)
            model_entity = provider.get_model_entity(model_name)
            features = model_entity.features
        except Exception as e:
            # 如果获取失败，记录日志但不影响返回
            current_app.logger.warning(f"获取模型特性失败: {str(e)}")

        # 5.提取信息并返回
        return {
            "id": str(app.id),
            "icon": app.icon,
            "name": app.name,
            "description": app.description,
            "app_config": {
                "opening_statement": app_config.get("opening_statement"),
                "opening_questions": app_config.get("opening_questions"),
                "suggested_after_answer": app_config.get("suggested_after_answer"),
                "features": features,
                "text_to_speech": app_config.get("text_to_speech"),
                "speech_to_text": app_config.get("speech_to_text"),
            },
        }

    def web_app_chat(
        self, token: str, req: WebAppChatReq, account: Account
    ) -> Generator:
        """根据传递的token凭证+请求与指定的WebApp进行对话"""
        # 1.获取WebApp应用并校验应用是否发布
        app = self.get_web_app(token)
        end_user = self.resolve_web_app_end_user(app=app, account=account)

        # 2.检测是否传递了会话id，如果传递了需要校验会话的归属信息
        if req.conversation_id.data:
            conversation = self.get(Conversation, req.conversation_id.data)
            if (
                not conversation
                or conversation.app_id != app.id
                or conversation.invoke_from != InvokeFrom.WEB_APP
                or conversation.created_by not in {end_user.id, account.id}
                or conversation.is_deleted is True
            ):
                raise ForbiddenException(
                    "该会话不存在，或者不属于当前应用/用户/调用方式"
                )
        else:
            # 3.如果没传递conversation_id表示新会话，这时候需要创建一个会话
            conversation = self.create(
                Conversation,
                **{
                    "app_id": app.id,
                    "name": "New Conversation",
                    "invoke_from": InvokeFrom.WEB_APP,
                    "created_by": end_user.id,
                },
            )

        # 4.获取校验后的运行时配置
        app_config = self.app_config_service.get_app_config(app)

        # 5.新建一条消息记录
        message = self.create(
            Message,
            app_id=app.id,
            conversation_id=conversation.id,
            invoke_from=InvokeFrom.WEB_APP,
            created_by=end_user.id,
            query=req.query.data,
            image_urls=req.image_urls.data,
            status=MessageStatus.NORMAL,
        )

        # 6.从语言模型管理器中加载大语言模型
        llm = self.language_model_service.load_language_model(
            app_config.get("model_config", {}),
            account_id=app.account_id,
        )

        # 7.完整历史交给 DeepAgents 原生 SummarizationMiddleware 管理
        history = load_history_messages(
            db=self.db,
            conversation=conversation,
        )

        # 8.将草稿配置中的tools转换成LangChain工具
        tools = self.app_config_service.get_langchain_tools_by_tools_config(
            app_config["tools"]
        )

        # 9.检测是否关联了知识库
        if app_config["datasets"]:
            # 10.构建LangChain知识库检索工具
            dataset_retrieval = (
                self.retrieval_service.create_langchain_tool_from_search(
                    flask_app=current_app._get_current_object(),
                    dataset_ids=[dataset["id"] for dataset in app_config["datasets"]],
                    account_id=app.account_id,
                    retrival_source=RetrievalSource.APP,
                    source_app_id=app.id,
                    **app_config["retrieval_config"],
                )
            )
            tools.append(dataset_retrieval)

        # 11.检测是否关联工作流，如果关联了工作流则将工作流构建成工具添加到tools中
        if app_config["workflows"]:
            workflow_tools = (
                self.app_config_service.get_langchain_tools_by_workflow_ids(
                    [workflow["id"] for workflow in app_config["workflows"]]
                )
            )
            tools.extend(workflow_tools)

        runtime = self.agent_runtime_service.prepare(
            account_id=app.account_id,
            app_id=app.id,
            conversation_id=conversation.id,
            created_by=end_user.id,
            end_user_id=end_user.id,
            query=req.query.data,
            skill_bindings=app_config["skills"],
            subagents=app_config["subagents"],
        )
        agent = BaseAgent(
            llm=llm,
            checkpointer=runtime.checkpointer,
            workspace_backend=runtime.workspace_backend,
            permissions=runtime.permissions,
            skill_sources=runtime.skill_sources,
            memory_sources=runtime.memory_sources,
            subagent_skill_sources=runtime.subagent_skill_sources,
            agent_config=AgentConfig(
                user_id=end_user.id,
                invoke_from=InvokeFrom.WEB_APP,
                app_name=app.name,
                app_description=app.description,
                preset_prompt=app_config["preset_prompt"],
                opening_statement=(
                    app_config["opening_statement"] if not history else ""
                ),
                subagents=app_config["subagents"],
                tools=tools,
                review_config=app_config["review_config"],
                account_id=app.account_id,
                app_id=app.id,
                conversation_id=conversation.id,
                end_user_id=end_user.id,
            ),
        )

        # 13.定义字典存储推理过程，并调用智能体获取消息
        # 注意：将历史消息和当前消息合并到messages字段中（AgentState已移除history字段）
        agent_thoughts = {}
        for agent_thought in agent.stream(
            {
                "history": history,
                "messages": [
                    llm.convert_to_human_message(
                        req.query.data,
                        req.image_urls.data,
                    )
                ],
            }
        ):
            # 14.提取thought以及answer
            event_id = str(agent_thought.id)

            # 15.将数据填充到agent_thought，便于存储到数据库服务中
            if agent_thought.event != QueueEvent.PING:
                # 16.除了agent_message数据为叠加，其他均为覆盖
                if agent_thought.event == QueueEvent.AGENT_MESSAGE:
                    if event_id not in agent_thoughts:
                        # 17.初始化智能体消息事件
                        agent_thoughts[event_id] = agent_thought
                    else:
                        # 18.叠加智能体消息
                        agent_thoughts[event_id] = agent_thoughts[event_id].model_copy(
                            update={
                                "thought": agent_thoughts[event_id].thought
                                + agent_thought.thought,
                                # 消息相关数据
                                "message": agent_thought.message,
                                "message_token_count": agent_thought.message_token_count,
                                "message_unit_price": agent_thought.message_unit_price,
                                "message_price_unit": agent_thought.message_price_unit,
                                # 答案相关数据
                                "answer": agent_thoughts[event_id].answer
                                + agent_thought.answer,
                                "answer_token_count": agent_thought.answer_token_count,
                                "answer_unit_price": agent_thought.answer_unit_price,
                                "answer_price_unit": agent_thought.answer_price_unit,
                                # Agent推理统计相关
                                "total_token_count": agent_thought.total_token_count,
                                "total_price": agent_thought.total_price,
                                "latency": agent_thought.latency,
                            }
                        )
                else:
                    # 19.处理其他类型事件的消息
                    agent_thoughts[event_id] = agent_thought
            data = {
                **agent_thought.model_dump(
                    include={
                        "event",
                        "thought",
                        "observation",
                        "tool",
                        "tool_input",
                        "answer",
                        "total_token_count",
                        "total_price",
                        "latency",
                    }
                ),
                "id": event_id,
                "conversation_id": str(conversation.id),
                "message_id": str(message.id),
                "task_id": str(agent_thought.task_id),
            }
            yield f"event: {agent_thought.event.value}\ndata:{json.dumps(data)}\n\n"

        # 20.将消息以及推理过程添加到数据库
        self.conversation_service.save_agent_thoughts(
            account_id=app.account_id,
            app_id=app.id,
            app_config=app_config,
            conversation_id=conversation.id,
            message_id=message.id,
            agent_thoughts=[agent_thought for agent_thought in agent_thoughts.values()],
        )
        answer = "".join(
            thought.answer
            for thought in agent_thoughts.values()
            if thought.event == QueueEvent.AGENT_MESSAGE
        )
        self.agent_runtime_service.extract_and_store_facts(
            account_id=app.account_id,
            app_id=app.id,
            end_user_id=end_user.id,
            conversation_id=conversation.id,
            message_id=message.id,
            query=req.query.data,
            answer=answer,
            llm=llm,
        )

    def stop_web_app_chat(self, token: str, task_id: UUID, account: Account):
        """根据传递的token+task_id停止与指定WebApp对话"""
        # 1.获取WebApp应用并校验应用是否发布
        app = self.get_web_app(token)
        end_user = self.resolve_web_app_end_user(app=app, account=account)

        # 2.调用智能体队列管理器停止特定任务
        AgentQueueManager.set_stop_flag(
            task_id,
            InvokeFrom.WEB_APP,
            end_user.id,
        )

    def get_conversations(
        self, token: str, is_pinned: bool, account: Account
    ) -> list[Conversation]:
        """根据传递的token+is_pinned+account获取指定账号在该WebApp下的会话列表数据"""
        # 1.获取WebApp应用并校验应用是否发布
        app = self.get_web_app(token)
        end_user = self.resolve_web_app_end_user(app=app, account=account)

        # 2.筛选过滤并查询数据
        conversations = (
            self.db.session.query(Conversation)
            .filter(
                Conversation.app_id == app.id,
                Conversation.created_by.in_([end_user.id, account.id]),
                Conversation.invoke_from == InvokeFrom.WEB_APP,
                Conversation.is_pinned == is_pinned,
                ~Conversation.is_deleted,
            )
            .order_by(desc("created_at"))
            .all()
        )

        return conversations
