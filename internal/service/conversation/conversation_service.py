from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from injector import inject
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from sqlalchemy import and_, desc, or_

from internal.entity.app_entity import AppStatus
from internal.entity.conversation_entity import (
    CONVERSATION_NAME_TEMPLATE,
    ConversationInfo,
    InvokeFrom,
    MessageStatus,
)
from internal.extension.logging_extension import logger
from pkg.sqlalchemy import SQLAlchemy
from internal.service.common.base_service import BaseService
from internal.service.agent.agent_conversation_cleanup_service import AgentConversationCleanupService
from internal.service.language_model.provider_api_key_service import ProviderApiKeyService
from internal.exception import ForbiddenException, NotFoundException
from internal.model import App, Conversation, EndUser, ConversationTurn, Account


@inject
@dataclass
class ConversationService(BaseService):
    """负责会话归属、消息创建和历史管理。"""

    db: SQLAlchemy

    provider_api_key_service: ProviderApiKeyService
    agent_cleanup_service: AgentConversationCleanupService | None = None

    def create_agent_turn(
        self, *, account_id: UUID, app_id: UUID, conversation_id: UUID, created_by: UUID,
        invoke_from: InvokeFrom, query: str, image_urls: tuple[str, ...],
        attachments: list[dict[str, Any]],
    ) -> ConversationTurn:
        """在调用方事务中校验会话并创建一次 Agent 业务消息。"""
        conversation = self.db.session.get(Conversation, conversation_id)
        if (
            conversation is None
            or conversation.app_id != app_id
            or conversation.is_deleted
        ):
            raise ForbiddenException("会话不存在或已删除")
        if conversation.owner_account_id is None:
            conversation.owner_account_id = account_id
        elif conversation.owner_account_id != account_id:
            raise ForbiddenException("会话不存在或不属于当前账号")

        # 服务端预创建的空会话在首条消息提交时获得业务名称。
        has_message = (
            self.db.session.query(ConversationTurn.id)
            .filter(
                ConversationTurn.conversation_id == conversation.id,
                ConversationTurn.is_deleted.is_(False),
            )
            .first()
            is not None
        )
        if not has_message:
            conversation.name = query[:255]
        conversation.updated_at = datetime.now()

        message = ConversationTurn(
            id=uuid4(),
            app_id=app_id,
            conversation_id=conversation.id,
            invoke_from=invoke_from,
            created_by=created_by,
            query=query,
            image_urls=list(image_urls),
            attachments=attachments,
            status=MessageStatus.NORMAL,
        )
        self.db.session.add(message)
        return message

    def create_service_api_conversation(
        self,
        *,
        app_id: UUID,
        account: Account,
        end_user_id: UUID | None = None,
    ) -> UUID:
        """校验应用和终端用户后创建账号拥有的 Service API 会话。"""
        with self.db.auto_commit():
            app = (
                self.db.session.query(App)
                .filter(
                    App.id == app_id,
                    App.account_id == account.id,
                    App.status == AppStatus.PUBLISHED,
                )
                .one_or_none()
            )
            if app is None:
                raise ForbiddenException("应用不存在、未发布或无权访问")

            if end_user_id is None:
                end_user = EndUser(app_id=app.id)
                self.db.session.add(end_user)
                self.db.session.flush()
            else:
                end_user = self.db.session.get(EndUser, end_user_id)
                if (
                    end_user is None
                    or end_user.app_id != app.id
                ):
                    raise ForbiddenException("终端用户不属于当前应用")

            conversation = Conversation(
                app_id=app.id,
                name="新的会话",
                invoke_from=InvokeFrom.SERVICE_API,
                created_by=end_user.id,
                owner_account_id=account.id,
            )
            self.db.session.add(conversation)
            self.db.session.flush()
            conversation_id = conversation.id
        return conversation_id

    def get_service_api_conversation(
        self,
        *,
        app_id: UUID,
        conversation_id: UUID,
        account: Account,
    ) -> Conversation:
        """校验开放 API 应用、会话归属及终端用户。"""
        app = (
            self.db.session.query(App)
            .filter(
                App.id == app_id,
                App.account_id == account.id,
                App.status == AppStatus.PUBLISHED,
            )
            .one_or_none()
        )
        if app is None:
            raise ForbiddenException("应用不存在、未发布或无权访问")
        # 已确认账号拥有该发布应用；旧 Service API 会话沿用应用所有权。
        conversation = (
            self.db.session.query(Conversation)
            .filter(
                Conversation.id == conversation_id,
                Conversation.app_id == app.id,
                Conversation.invoke_from == InvokeFrom.SERVICE_API,
                Conversation.is_deleted.is_(False),
                or_(Conversation.owner_account_id == account.id, Conversation.owner_account_id.is_(None)),
            )
            .one_or_none()
        )
        if conversation is None:
            raise NotFoundException("该会话不存在或被删除，请核实后重试")
        end_user = (
            self.db.session.query(EndUser)
            .filter(
                EndUser.id == conversation.created_by,
                EndUser.app_id == app.id,
            )
            .one_or_none()
        )
        if end_user is None:
            raise ForbiddenException("服务 API 会话终端用户无效")
        return conversation

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
            conversation = Conversation(
                app_id=app.id,
                name="New Conversation",
                invoke_from=InvokeFrom.DEBUGGER,
                created_by=account_id,
                owner_account_id=account_id,
            )
            self.db.session.add(conversation)
            self.db.session.flush()
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
            conversation = Conversation(
                app_id=app_id,
                name="New Conversation",
                invoke_from=InvokeFrom.ASSISTANT_AGENT,
                created_by=account_id,
                owner_account_id=account_id,
            )
            self.db.session.add(conversation)
            self.db.session.flush()
        return conversation

    def _load_model_from_config(
        self, model_config: dict = None, account_id: UUID = None
    ):
        """从模型配置加载模型实例，失败时回退到默认模型。"""
        from internal.service.language_model.language_model_service import LanguageModelService
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
        from internal.service.language_model.language_model_service import LanguageModelService
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
        from internal.service.language_model.language_model_service import LanguageModelService
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

    def get_conversation(
        self,
        conversation_id: UUID,
        account: Account,
        *,
        app_id: UUID | None = None,
    ) -> Conversation:
        """通用会话与 Run 详情按账号归属读取；入口专属的来源限制由各自服务执行。"""
        query = self.db.session.query(Conversation).filter(
            Conversation.id == conversation_id,
            Conversation.is_deleted.is_(False),
        )
        if app_id is not None:
            query = query.filter(Conversation.app_id == app_id)
        conversation = query.one_or_none()
        if conversation is None:
            raise NotFoundException("该会话不存在或被删除，请核实后重试")

        # owner_account_id 是唯一的新数据授权依据；后续条件只兼容旧数据。
        if conversation.owner_account_id == account.id:
            return conversation
        if conversation.owner_account_id is not None:
            raise NotFoundException("该会话不存在或被删除，请核实后重试")

        legacy_owned = conversation.created_by == account.id
        if not legacy_owned and conversation.invoke_from == InvokeFrom.WEB_APP:
            legacy_owned = (
                self.db.session.query(EndUser.id)
                .filter(
                    EndUser.id == conversation.created_by,
                    EndUser.app_id == conversation.app_id,
                    EndUser.account_id == account.id,
                )
                .one_or_none()
                is not None
            )
        if not legacy_owned and conversation.invoke_from == InvokeFrom.SERVICE_API:
            legacy_owned = (
                self.db.session.query(App.id)
                .filter(
                    App.id == conversation.app_id,
                    App.account_id == account.id,
                )
                .one_or_none()
                is not None
            )
        if not legacy_owned:
            raise NotFoundException("该会话不存在或被删除，请核实后重试")
        return conversation

    def get_turn_page(
        self,
        *,
        conversation_id: UUID,
        before: UUID | None = None,
        page_size: int = 20,
    ) -> tuple[list[ConversationTurn], bool, UUID | None]:
        """按 created_at 与消息 ID 的稳定游标读取一页业务消息。"""
        limit = min(max(page_size, 1), 50)
        query = self.db.session.query(ConversationTurn).filter(
            ConversationTurn.conversation_id == conversation_id,
            ConversationTurn.is_deleted.is_(False),
        )
        if before is not None:
            cursor = (
                self.db.session.query(ConversationTurn.id, ConversationTurn.created_at)
                .filter(
                    ConversationTurn.id == before,
                    ConversationTurn.conversation_id == conversation_id,
                    ConversationTurn.is_deleted.is_(False),
                )
                .one_or_none()
            )
            if cursor is None:
                raise NotFoundException("历史消息游标不存在")
            query = query.filter(
                or_(
                    ConversationTurn.created_at < cursor.created_at,
                    and_(
                        ConversationTurn.created_at == cursor.created_at,
                        ConversationTurn.id < cursor.id,
                    ),
                )
            )
        descending_rows = (
            query.order_by(ConversationTurn.created_at.desc(), ConversationTurn.id.desc())
            .limit(limit + 1)
            .all()
        )
        has_more = len(descending_rows) > limit
        page_rows = descending_rows[:limit]
        return (
            list(reversed(page_rows)),
            has_more,
            page_rows[-1].id if has_more and page_rows else None,
        )

    def get_account_message_page(
        self,
        *,
        conversation_id: UUID,
        account: Account,
        before: UUID | None = None,
        page_size: int = 20,
    ) -> tuple[list[ConversationTurn], bool, UUID | None]:
        """鉴权后读取当前账号的一页业务消息。"""
        conversation = self.get_conversation(conversation_id, account)
        return self.get_turn_page(
            conversation_id=conversation.id,
            before=before,
            page_size=page_size,
        )

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
            account_id=account.id,
            app_id=conversation.app_id,
            conversation_id=conversation.id,
        )
        self.update(conversation, is_deleted=True)

        return conversation

    def delete_message(
        self, conversation_id: UUID, message_id: UUID, account: Account
    ) -> ConversationTurn:
        """根据会话 ID 和消息 ID 删除指定消息。"""
        # 1.获取会话记录并校验访问权限。
        conversation = self.get_conversation(conversation_id, account)

        # 2.消息权限继承会话归属，不比较可能记录 EndUser 的 created_by。
        message = (
            self.db.session.query(ConversationTurn)
            .filter(
                ConversationTurn.id == message_id,
                ConversationTurn.conversation_id == conversation.id,
                ConversationTurn.is_deleted.is_(False),
            )
            .one_or_none()
        )
        if message is None:
            raise NotFoundException("该会话下不存在该消息，请核实后重试")

        # 3.校验通过后标记消息已删除。
        if self.agent_cleanup_service is None:
            raise RuntimeError("会话清理服务未配置")
        self.agent_cleanup_service.cleanup_message(
            conversation_id=conversation.id,
            message_id=message.id,
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
