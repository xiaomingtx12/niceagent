import logging
from dataclasses import dataclass
from datetime import datetime
from threading import Thread
from typing import Any
from uuid import UUID

from flask import Flask, current_app
from injector import inject
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from sqlalchemy import desc
from sqlalchemy.orm import joinedload

from internal.core.agent_v2.agents.models import AgentThought, QueueEvent
from internal.entity.conversation_entity import (
    CONVERSATION_NAME_TEMPLATE,
    ConversationInfo,
    InvokeFrom,
    MessageStatus,
)
from pkg.paginator import Paginator
from pkg.sqlalchemy import SQLAlchemy
from .base_service import BaseService
from .agent_conversation_cleanup_service import AgentConversationCleanupService
from .provider_api_key_service import ProviderApiKeyService
from ..exception import NotFoundException
from ..model import Conversation, Message, MessageAgentThought, Account
from ..schema.conversation_schema import GetConversationMessagesWithPageReq


@inject
@dataclass
class ConversationService(BaseService):
    """会话服务"""

    db: SQLAlchemy
    provider_api_key_service: ProviderApiKeyService
    agent_cleanup_service: AgentConversationCleanupService | None = None

    def _load_model_from_config(
        self, model_config: dict = None, account_id: UUID = None
    ):
        """从模型配置加载模型实例，如果失败则使用默认模型"""
        from internal.service.language_model_service import LanguageModelService
        from injector import Injector

        try:
            if model_config and account_id:
                # 使用应用配置的模型
                language_model_service = Injector().get(LanguageModelService)
                return language_model_service.load_language_model(
                    model_config, account_id
                )
        except Exception as e:
            logging.warning(f"加载配置模型失败: {str(e)}，使用默认模型")

    @classmethod
    def generate_conversation_name(
        cls, query: str, account_id: UUID = None, model_config: dict = None
    ) -> str:
        """根据传递的query生成对应的会话名字，并且语言与用户的输入保持一致"""
        from internal.service.language_model_service import LanguageModelService
        from injector import Injector

        # 1.加载模型：如果未传入model_config，使用默认的tongyi配置（需要用户自己的API Key）
        if not model_config:
            model_config = {"provider": "tongyi", "model": "qwen-plus", "parameters": {}}

        llm = None
        try:
            if account_id:
                language_model_service = Injector().get(LanguageModelService)
                llm = language_model_service.load_language_model(
                    model_config, account_id
                )
        except Exception as e:
            logging.warning(f"加载模型失败: {str(e)}")
            return "新的会话"

        if llm is None:
            logging.warning("模型加载结果为None，跳过会话命名")
            return "新的会话"

        # 2.创建prompt
        prompt = ChatPromptTemplate.from_messages(
            [("system", CONVERSATION_NAME_TEMPLATE), ("human", "{query}")]
        )

        # 3.构建结构化输出
        structured_llm = llm.with_structured_output(ConversationInfo)

        # 4.构建链应用
        chain = prompt | structured_llm

        # 5.提取并整理query，截取长度过长的部分
        if len(query) > 2000:
            query = query[:300] + "...[TRUNCATED]..." + query[-300:]
        query = query.replace("\n", " ")

        # 6.调用链并获取会话信息
        conversation_info = chain.invoke({"query": query})

        # 7.提取会话名称
        name = "新的会话"
        try:
            if conversation_info and hasattr(conversation_info, "subject"):
                name = conversation_info.subject
        except Exception as e:
            logging.exception(
                f"提取会话名称出错, conversation_info: {conversation_info}, 错误信息: {str(e)}"
            )
        if len(name) > 75:
            name = name[:75] + "..."

        return name

    @classmethod
    def generate_suggested_questions(
        cls, histories: str, account_id: UUID = None, model_config: dict = None
    ) -> list[str]:
        """根据传递的历史信息生成最多不超过3个的建议问题"""
        import json as json_module
        from internal.service.language_model_service import LanguageModelService
        from injector import Injector

        try:
            # 1.加载模型：如果未传入model_config，使用默认的tongyi配置（需要用户自己的API Key）
            if not model_config:
                model_config = {"provider": "tongyi", "model": "qwen-plus", "parameters": {}}

            llm = None
            try:
                if account_id:
                    language_model_service = Injector().get(LanguageModelService)
                    llm = language_model_service.load_language_model(
                        model_config, account_id
                    )
            except Exception as e:
                logging.warning(f"加载模型失败: {str(e)}")
                return []

            if llm is None:
                logging.warning("模型加载结果为None，跳过建议问题生成")
                return []

            # 2.创建prompt（不使用with_structured_output，直接让模型输出JSON数组）
            prompt = ChatPromptTemplate.from_messages(
                [
                    (
                        "system",
                        "你是一个建议问题生成助手。请根据用户传递的对话历史，预测用户接下来最可能会问的3个问题。"
                        "\n每个问题保持在50个字符以内。"
                        "\n\n你必须严格按照以下JSON数组格式输出，不要输出任何其他内容："
                        '\n["问题1", "问题2", "问题3"]',
                    ),
                    ("human", "{histories}"),
                ]
            )

            # 3.构建链应用（使用StrOutputParser直接获取文本输出）
            chain = prompt | llm | StrOutputParser()

            # 4.调用链并获取原始文本
            raw_output = chain.invoke({"histories": histories})

            # 5.解析JSON数组
            questions = []
            if raw_output:
                # 去除可能的markdown代码块标记
                text = raw_output.strip()
                if text.startswith("```"):
                    # 移除 ```json 或 ``` 开头和结尾
                    lines = text.split("\n")
                    lines = [l for l in lines if not l.strip().startswith("```")]
                    text = "\n".join(lines).strip()

                try:
                    parsed = json_module.loads(text)
                    if isinstance(parsed, list):
                        questions = [str(q) for q in parsed if isinstance(q, str)]
                except json_module.JSONDecodeError:
                    logging.warning(f"建议问题JSON解析失败, raw_output: {raw_output[:200]}")

            # 6.限制问题数量
            if len(questions) > 3:
                questions = questions[:3]

            return questions

        except Exception as e:
            logging.exception(
                f"生成建议问题失败, histories: {histories[:200]}..., 错误信息: {str(e)}"
            )
            # 返回空列表，避免影响用户体验
            return []

    def save_agent_thoughts(
        self,
        account_id: UUID,
        app_id: UUID,
        app_config: dict[str, Any],
        conversation_id: UUID,
        message_id: UUID,
        agent_thoughts: list[AgentThought],
    ) -> None:
        """持久化 Agent V2 轨迹事件并更新父消息。"""
        # 1.定义变量存储推理位置及总耗时
        position = 0
        latency = 0

        # 2.在子线程中重新查询conversation以及message，确保对象会被子线程的会话管理到
        conversation = self.get(Conversation, conversation_id)
        message = self.get(Message, message_id)

        # 3.循环遍历所有的智能体推理过程执行存储操作
        for agent_thought in agent_thoughts:
            # 4.存储长期记忆召回、推理、消息、动作、知识库检索等步骤
            if agent_thought.event in [
                QueueEvent.AGENT_THOUGHT,
                QueueEvent.AGENT_MESSAGE,
                QueueEvent.AGENT_ACTION,
                QueueEvent.DATASET_RETRIEVAL,
                QueueEvent.TODO_UPDATE,
                QueueEvent.SUB_AGENT_DELEGATE,
                QueueEvent.SUB_AGENT_END,
            ]:
                # 5.更新位置及总耗时
                position += 1
                latency += agent_thought.latency

                # 6.创建智能体消息推理步骤
                self.create(
                    MessageAgentThought,
                    app_id=app_id,
                    conversation_id=conversation.id,
                    message_id=message.id,
                    invoke_from=message.invoke_from,
                    created_by=account_id,
                    position=position,
                    event=agent_thought.event,
                    thought=agent_thought.thought,
                    observation=agent_thought.observation,
                    tool=agent_thought.tool,
                    tool_input=agent_thought.tool_input,
                    agent_id=agent_thought.agent_id,
                    agent_type=agent_thought.agent_type.value,
                    parent_agent_id=agent_thought.parent_agent_id,
                    delegation_id=agent_thought.delegation_id,
                    task_id=agent_thought.task_id,
                    todos=[todo.model_dump(mode="json") for todo in agent_thought.todos],
                    # 消息相关数据
                    message=agent_thought.message,
                    message_token_count=agent_thought.message_token_count,
                    message_unit_price=agent_thought.message_unit_price,
                    message_price_unit=agent_thought.message_price_unit,
                    # 答案相关字段
                    answer=agent_thought.answer,
                    answer_token_count=agent_thought.answer_token_count,
                    answer_unit_price=agent_thought.answer_unit_price,
                    answer_price_unit=agent_thought.answer_price_unit,
                    # Agent推理统计相关
                    total_token_count=agent_thought.total_token_count,
                    total_price=agent_thought.total_price,
                    latency=agent_thought.latency,
                )

            # 7.检测事件是否为Agent_message
            if agent_thought.event == QueueEvent.AGENT_MESSAGE:
                # 8.更新消息信息
                self.update(
                    message,
                    # 消息相关字段
                    message=agent_thought.message,
                    message_token_count=agent_thought.message_token_count,
                    message_unit_price=agent_thought.message_unit_price,
                    message_price_unit=agent_thought.message_price_unit,
                    # 答案相关字段
                    answer=agent_thought.answer,
                    answer_token_count=agent_thought.answer_token_count,
                    answer_unit_price=agent_thought.answer_unit_price,
                    answer_price_unit=agent_thought.answer_price_unit,
                    # Agent推理统计相关
                    total_token_count=agent_thought.total_token_count,
                    total_price=agent_thought.total_price,
                    latency=latency,
                )

                # 9.处理生成新会话名称。历史摘要由 DeepAgents 默认
                # SummarizationMiddleware + workspace offload 接管。
                if conversation.is_new:
                    Thread(
                        target=self._generate_conversation_name_and_update,
                        kwargs={
                            "flask_app": current_app._get_current_object(),
                            "conversation_id": conversation.id,
                            "query": message.query,
                            "account_id": account_id,
                            "model_config": app_config.get("model_config"),
                        },
                    ).start()

            # 10.判断是否为停止或者错误，如果是则需要更新消息状态
            if agent_thought.event in [
                QueueEvent.TIMEOUT,
                QueueEvent.STOP,
                QueueEvent.ERROR,
            ]:
                self.update(
                    message,
                    status=agent_thought.event,
                    error=agent_thought.observation,
                )
                break

    def _generate_conversation_name_and_update(
        self,
        flask_app: Flask,
        conversation_id: UUID,
        query: str,
        account_id: UUID,
        model_config: dict = None,
    ) -> None:
        """生成会话名字并更新"""
        with flask_app.app_context():
            # 1.根据会话id获取会话
            conversation = self.get(Conversation, conversation_id)

            # 3.计算获取新会话名字
            new_conversation_name = self.generate_conversation_name(
                query, account_id, model_config
            )

            # 4.调用更新服务更新会话名称
            self.update(
                conversation,
                name=new_conversation_name,
            )

    def get_conversation(self, conversation_id: UUID, account: Account) -> Conversation:
        """根据传递的会话id+account，获取指定的会话信息"""
        # 1.根据conversation_id查询会话记录
        conversation = self.get(Conversation, conversation_id)
        if (
            not conversation
            or conversation.created_by != account.id
            or conversation.is_deleted
        ):
            raise NotFoundException("该会话不存在或被删除，请核实后重试")

        # 2.校验通过返回会话
        return conversation

    def get_message(self, message_id: UUID, account: Account) -> Message:
        """根据传递的消息id+账号，获取指定的消息"""
        # 1.根据message_id查询消息记录
        message = self.get(Message, message_id)
        if not message or message.created_by != account.id or message.is_deleted:
            raise NotFoundException("该消息不存在或被删除，请核实后重试")

        # 2.校验通过返回消息
        return message

    def get_conversation_messages_with_page(
        self,
        conversation_id: UUID,
        req: GetConversationMessagesWithPageReq,
        account: Account,
    ) -> tuple[list[Message], Paginator]:
        """根据传递的会话id+请求数据，获取当前账号下该会话的消息分页列表数据"""
        # 1.获取会话并校验权限
        conversation = self.get_conversation(conversation_id, account)

        # 2.构建分页器并设置游标条件
        paginator = Paginator(db=self.db, req=req)
        filters = []
        if req.created_at.data:
            # 3.将时间戳转换成DateTime
            created_at_datetime = datetime.fromtimestamp(req.created_at.data)
            filters.append(Message.created_at <= created_at_datetime)

        # 4.执行分页并查询数据
        messages = paginator.paginate(
            self.db.session.query(Message)
            .options(joinedload(Message.agent_thoughts))
            .filter(
                Message.conversation_id == conversation.id,
                Message.status.in_([MessageStatus.STOP, MessageStatus.NORMAL]),
                Message.answer != "",
                ~Message.is_deleted,
                *filters,
            )
            .order_by(desc("created_at"))
        )

        return messages, paginator

    def delete_conversation(
        self, conversation_id: UUID, account: Account
    ) -> Conversation:
        """根据传递的会话id+账号删除指定的会话记录"""
        # 1.获取会话记录并校验权限
        conversation = self.get_conversation(conversation_id, account)

        # 2.更新会话的删除状态
        if self.agent_cleanup_service is None:
            raise RuntimeError("会话清理服务未配置")
        self.agent_cleanup_service.cleanup_conversation(
            account_id=account.id,
            app_id=conversation.app_id,
            conversation_id=conversation.id,
        )
        self.update(conversation, is_deleted=True)

        return conversation

    def delete_message(
        self, conversation_id: UUID, message_id: UUID, account: Account
    ) -> Message:
        """根据传递的会话id+消息id删除指定的消息记录"""
        # 1.获取会话记录并校验权限
        conversation = self.get_conversation(conversation_id, account)

        # 2.获取消息并校验权限
        message = self.get_message(message_id, account)

        # 3.判断消息和会话是否关联
        if conversation.id != message.conversation_id:
            raise NotFoundException("该会话下不存在该消息，请核实后重试")

        # 4.校验通过修改消息is_deleted属性标记删除
        if self.agent_cleanup_service is None:
            raise RuntimeError("会话清理服务未配置")
        self.agent_cleanup_service.cleanup_message(
            account_id=account.id,
            app_id=conversation.app_id,
            conversation_id=conversation.id,
            message_id=message.id,
        )
        self.update(message, is_deleted=True)

        return message

    def update_conversation(
        self, conversation_id: UUID, account: Account, **kwargs
    ) -> Conversation:
        """根据传递的会话id+账号+kwargs更新会话信息"""
        # 1.获取会话记录并校验权限
        conversation = self.get_conversation(conversation_id, account)

        # 2.更新会话信息
        self.update(conversation, **kwargs)

        return conversation
