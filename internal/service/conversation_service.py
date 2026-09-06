from dataclasses import dataclass
from datetime import datetime
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from injector import inject
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from sqlalchemy import desc

from internal.entity.conversation_entity import (
    CONVERSATION_NAME_TEMPLATE,
    ConversationInfo,
    InvokeFrom,
    MessageStatus,
)
from internal.extension.logging_extension import logger
from pkg.paginator import Paginator
from pkg.sqlalchemy import SQLAlchemy
from internal.schema.agent_runtime_schema import (
    AgentConversationCleanupReq,
    AgentMessageCleanupReq,
)
from .base_service import BaseService
from .agent_conversation_cleanup_service import AgentConversationCleanupService
from .provider_api_key_service import ProviderApiKeyService
from ..exception import NotFoundException
from ..model import App, Conversation, Message, Account
from ..schema.conversation_schema import GetConversationMessagesWithPageReq


def _usage_detail(details: Mapping[str, Any], name: str) -> int:
    """读取 Token 明细并统一转换为整数。"""
    aliases = {
        "cached": ("cached", "cached_tokens", "cache_read", "cache_read_tokens"),
        "reasoning": ("reasoning", "reasoning_tokens"),
    }
    for alias in aliases.get(name, (name,)):
        value = details.get(alias, 0)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


@inject
@dataclass
class ConversationService(BaseService):
    """会话服务，负责 Agent 轨迹、消息状态和 Run 用量持久化。"""

    db: SQLAlchemy

    @staticmethod
    def build_run_usage_updates(
        run_usage: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """把统一 Run 用量映射为 Message 统计字段。"""
        # 1.没有 Run 用量时不覆盖消息原有统计。
        if not run_usage:
            return {}
        # 2.规范化输入、输出 Token 明细，兼容缺失或异常结构。
        input_details = run_usage.get("input_details", {})
        output_details = run_usage.get("output_details", {})
        if not isinstance(input_details, Mapping):
            input_details = {}
        if not isinstance(output_details, Mapping):
            output_details = {}

        # 3.先写入调用次数、Trace 和按模型拆分的原始统计。
        updates: dict[str, Any] = {
            "trace_id": run_usage.get("trace_id"),
            "model_call_count": int(run_usage.get("model_call_count", 0) or 0),
            "tool_call_count": int(run_usage.get("tool_call_count", 0) or 0),
            "token_usage": dict(run_usage.get("models", {}) or {}),
        }
        if run_usage.get("usage_present"):
            # 4.补充输入、输出、缓存、推理和总 Token 统计。
            updates.update(
                {
                    "input_token_count": int(run_usage.get("input_tokens", 0) or 0),
                    "output_token_count": int(run_usage.get("output_tokens", 0) or 0),
                    "cached_token_count": _usage_detail(
                        input_details,
                        "cached",
                    ),
                    "reasoning_token_count": max(
                        _usage_detail(input_details, "reasoning"),
                        _usage_detail(output_details, "reasoning"),
                    ),
                    "total_token_count": int(run_usage.get("total_tokens", 0) or 0),
                }
            )
        return updates

    provider_api_key_service: ProviderApiKeyService
    agent_cleanup_service: AgentConversationCleanupService | None = None

    def create_conversation(
        self,
        *,
        app_id: UUID,
        invoke_from: InvokeFrom,
        created_by: UUID,
        owner_account_id: UUID | None = None,
        name: str = "新的会话",
    ) -> Conversation:
        """在调用方事务中创建由服务端分配 ID 的会话。"""
        return self.create_in_transaction(
            Conversation,
            app_id=app_id,
            name=name[:255],
            invoke_from=invoke_from,
            created_by=created_by,
            owner_account_id=owner_account_id,
        )

    def get_or_create_debug_conversation(
        self,
        *,
        app: App,
        account_id: UUID,
    ) -> Conversation:
        """返回应用的调试会话；首次打开时由后端创建。"""
        if app.account_id != account_id:
            raise NotFoundException("应用不存在或无权访问")

        conversation = (
            self.db.session.query(Conversation)
            .filter(
                Conversation.app_id == app.id,
                Conversation.invoke_from == InvokeFrom.DEBUGGER,
                Conversation.created_by == account_id,
                Conversation.is_deleted.is_(False),
            )
            .order_by(desc(Conversation.created_at), desc(Conversation.id))
            .first()
        )
        if conversation is None:
            conversation = self.create_conversation(
                app_id=app.id,
                invoke_from=InvokeFrom.DEBUGGER,
                created_by=account_id,
                owner_account_id=account_id,
                name="New Conversation",
            )
        return conversation

    def get_or_create_assistant_conversation(
        self,
        *,
        account_id: UUID,
        app_id: UUID,
    ) -> Conversation:
        """按会话归属返回账号的辅助 Agent 会话，首次使用时创建。"""
        conversation = (
            self.db.session.query(Conversation)
            .filter(
                Conversation.app_id == app_id,
                Conversation.invoke_from == InvokeFrom.ASSISTANT_AGENT,
                Conversation.created_by == account_id,
                Conversation.is_deleted.is_(False),
            )
            .order_by(desc(Conversation.created_at), desc(Conversation.id))
            .first()
        )
        if conversation is None:
            conversation = self.create_conversation(
                app_id=app_id,
                invoke_from=InvokeFrom.ASSISTANT_AGENT,
                created_by=account_id,
                owner_account_id=account_id,
                name="New Conversation",
            )
        return conversation

    def _load_model_from_config(
        self, model_config: dict = None, account_id: UUID = None
    ):
        """从模型配置加载模型实例，失败时回退到默认模型。"""
        from internal.service.language_model_service import LanguageModelService
        from app.http.module import injector

        try:
            if model_config and account_id:
                # 使用应用配置指定的模型。
                language_model_service = injector.get(LanguageModelService)
                return language_model_service.load_language_model(
                    model_config, account_id
                )
        except Exception as e:
            logger.warning("加载配置模型失败: %s，使用默认模型", e)

    @classmethod
    def generate_conversation_name(
        cls, query: str, account_id: UUID = None, model_config: dict = None
    ) -> str:
        """根据用户问题生成会话名称，并保持与输入相同的语言。"""
        from internal.service.language_model_service import LanguageModelService
        from app.http.module import injector

        llm = None
        try:
            if account_id:
                language_model_service = injector.get(LanguageModelService)
                if not model_config:
                    model_config = language_model_service.get_default_model_config(
                        account_id
                    )
                llm = language_model_service.load_language_model(
                    model_config, account_id
                )
        except Exception as e:
            logger.warning("加载模型失败: %s", e)
            return "新的会话"

        if llm is None:
            logger.warning("模型加载结果为None，跳过会话命名")
            return "新的会话"

        # 2.创建会话命名提示词。
        prompt = ChatPromptTemplate.from_messages(
            [("system", CONVERSATION_NAME_TEMPLATE), ("human", "{query}")]
        )

        # 3.配置结构化输出。
        structured_llm = llm.with_structured_output(ConversationInfo)

        # 4.组装模型调用链。
        chain = prompt | structured_llm

        # 5.提取并整理用户问题，截断过长内容。
        if len(query) > 2000:
            query = query[:300] + "...[TRUNCATED]..." + query[-300:]
        query = query.replace("\n", " ")

        # 6.调用模型链并获取会话信息。
        conversation_info = chain.invoke({"query": query})

        # 7.提取并规范会话名称。
        name = "新的会话"
        try:
            if conversation_info and hasattr(conversation_info, "subject"):
                name = conversation_info.subject
        except Exception as e:
            logger.exception(
                "提取会话名称出错, conversation_info: %s, 错误信息: %s",
                conversation_info,
                e,
            )
        if len(name) > 75:
            name = name[:75] + "..."

        return name

    @classmethod
    def generate_suggested_questions(
        cls, histories: str, account_id: UUID = None, model_config: dict = None
    ) -> list[str]:
        """根据历史对话生成最多 3 个建议问题。"""
        import json as json_module
        from internal.service.language_model_service import LanguageModelService
        from app.http.module import injector

        try:
            llm = None
            try:
                if account_id:
                    language_model_service = injector.get(LanguageModelService)
                    if not model_config:
                        model_config = language_model_service.get_default_model_config(
                            account_id
                        )
                    llm = language_model_service.load_language_model(
                        model_config, account_id
                    )
            except Exception as e:
                logger.warning("加载模型失败: %s", e)
                return []

            if llm is None:
                logger.warning("模型加载结果为None，跳过建议问题生成")
                return []

            # 2.创建提示词，直接要求模型输出 JSON 数组。
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

            # 3.组装调用链，使用 StrOutputParser 获取文本结果。
            chain = prompt | llm | StrOutputParser()

            # 4.调用模型链并获取原始文本。
            raw_output = chain.invoke({"histories": histories})

            # 5.解析 JSON 数组。
            questions = []
            if raw_output:
                # 去除可能存在的 Markdown 代码块标记。
                text = raw_output.strip()
                if text.startswith("```"):
                    # 移除 ```json 或 ``` 的起止标记。
                    lines = text.split("\n")
                    lines = [l for l in lines if not l.strip().startswith("```")]
                    text = "\n".join(lines).strip()

                try:
                    parsed = json_module.loads(text)
                    if isinstance(parsed, list):
                        questions = [str(q) for q in parsed if isinstance(q, str)]
                except json_module.JSONDecodeError:
                    logger.warning(
                        "建议问题JSON解析失败, raw_output: %s",
                        raw_output[:200],
                    )

            # 6.限制建议问题数量。
            if len(questions) > 3:
                questions = questions[:3]

            return questions

        except Exception as e:
            logger.exception(
                "生成建议问题失败, histories: %s..., 错误信息: %s",
                histories[:200],
                e,
            )
            # 返回空列表，避免影响主流程。
            return []

    def get_conversation(self, conversation_id: UUID, account: Account) -> Conversation:
        """根据会话 ID 和账号获取指定会话。"""
        # 1.根据会话 ID 查询会话记录。
        conversation = self.get(Conversation, conversation_id)
        if (
            not conversation
            or conversation.created_by != account.id
            or conversation.is_deleted
        ):
            raise NotFoundException("该会话不存在或被删除，请核实后重试")

        # 2.校验通过后返回会话。
        return conversation

    def get_message(self, message_id: UUID, account: Account) -> Message:
        """根据消息 ID 和账号获取指定消息。"""
        # 1.根据消息 ID 查询消息记录。
        message = self.get(Message, message_id)
        if not message or message.created_by != account.id or message.is_deleted:
            raise NotFoundException("该消息不存在或被删除，请核实后重试")

        # 2.校验通过后返回消息。
        return message

    def get_conversation_messages_with_page(
        self,
        conversation_id: UUID,
        req: GetConversationMessagesWithPageReq,
        account: Account,
    ) -> tuple[list[Message], Paginator]:
        """根据会话和分页请求获取当前账号可见的消息列表。"""
        # 1.获取会话并校验访问权限。
        conversation = self.get_conversation(conversation_id, account)

        # 2.构建分页器并设置游标条件。
        paginator = Paginator(db=self.db, req=req)
        filters = []
        if req.created_at.data:
            # 3.将时间戳转换为日期时间对象。
            created_at_datetime = datetime.fromtimestamp(req.created_at.data)
            filters.append(Message.created_at <= created_at_datetime)

        # 4.执行分页查询。
        messages = paginator.paginate(
            self.db.session.query(Message)
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
        """根据会话 ID 和账号删除指定会话。"""
        # 1.获取会话记录并校验访问权限。
        conversation = self.get_conversation(conversation_id, account)

        # 2.清理运行时资源并更新会话删除状态。
        if self.agent_cleanup_service is None:
            raise RuntimeError("会话清理服务未配置")
        self.agent_cleanup_service.cleanup_conversation(
            AgentConversationCleanupReq(
                account_id=account.id,
                app_id=conversation.app_id,
                conversation_id=conversation.id,
            )
        )
        self.update(conversation, is_deleted=True)

        return conversation

    def delete_message(
        self, conversation_id: UUID, message_id: UUID, account: Account
    ) -> Message:
        """根据会话 ID 和消息 ID 删除指定消息。"""
        # 1.获取会话记录并校验访问权限。
        conversation = self.get_conversation(conversation_id, account)

        # 2.获取消息并校验访问权限。
        message = self.get_message(message_id, account)

        # 3.确认消息属于当前会话。
        if conversation.id != message.conversation_id:
            raise NotFoundException("该会话下不存在该消息，请核实后重试")

        # 4.校验通过后标记消息已删除。
        if self.agent_cleanup_service is None:
            raise RuntimeError("会话清理服务未配置")
        self.agent_cleanup_service.cleanup_message(
            AgentMessageCleanupReq(
                account_id=account.id,
                app_id=conversation.app_id,
                conversation_id=conversation.id,
                message_id=message.id,
            )
        )
        self.update(message, is_deleted=True)

        return message

    def update_conversation(
        self, conversation_id: UUID, account: Account, **kwargs
    ) -> Conversation:
        """根据会话 ID、账号和更新字段修改会话信息。"""
        # 1.获取会话记录并校验访问权限。
        conversation = self.get_conversation(conversation_id, account)

        # 2.更新会话信息。
        self.update(conversation, **kwargs)

        return conversation
