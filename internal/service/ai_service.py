import json
from dataclasses import dataclass
from typing import Generator
from uuid import UUID

from injector import inject
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from internal.entity.ai_entity import OPTIMIZE_PROMPT_TEMPLATE
from internal.exception import ForbiddenException
from internal.model import Account, Message
from internal.model import App
from pkg.sqlalchemy import SQLAlchemy
from .base_service import BaseService
from .conversation_service import ConversationService
from .language_model_service import LanguageModelService
from .provider_api_key_service import ProviderApiKeyService


@inject
@dataclass
class AIService(BaseService):
    """AI服务"""

    db: SQLAlchemy
    conversation_service: ConversationService
    provider_api_key_service: ProviderApiKeyService
    language_model_service: LanguageModelService

    def generate_suggested_questions_from_message_id(
        self, message_id: UUID, account: Account
    ) -> list[str]:
        """根据传递的消息id+账号生成建议问题列表"""

        # 1.查询消息并校验权限信息
        message = self.get(Message, message_id)
        if not message or message.created_by != account.id:
            raise ForbiddenException("该条消息不存在或无权限")

        # 2.构建对话历史列表
        histories = f"Human: {message.query}\nAI: {message.answer}"

        # 3.获取应用模型配置加载模型去生成
        app = self.get(App, message.app_id)
        model_config = None
        if app:
            draft_config = app.draft_app_config
            if draft_config and draft_config.model_config:
                model_config = draft_config.model_config

        # 4.调用服务生成建议问题
        return self.conversation_service.generate_suggested_questions(
            histories, account.id, model_config
        )

    def optimize_prompt(
        self, prompt: str, account: Account, app_id: UUID = None
    ) -> Generator[str, None, None]:
        """根据传递的prompt进行优化生成（实例方法）"""

        # 1. 加载模型
        app = self.get(App, app_id)
        model_config = None
        if app:
            draft_config = app.draft_app_config
            if draft_config and draft_config.model_config:
                model_config = draft_config.model_config

        # 如果model_config为None，使用默认的tongyi配置（需要用户自己的API Key）
        if not model_config:
            model_config = {"provider": "tongyi", "model": "qwen-plus", "parameters": {}}

        llm = self.language_model_service.load_language_model(model_config, account.id)

        # 2.构建优化prompt的提示词模板
        prompt_template = ChatPromptTemplate.from_messages(
            [("system", OPTIMIZE_PROMPT_TEMPLATE), ("human", "{prompt}")]
        )

        # 3.组装优化链
        optimize_chain = prompt_template | llm | StrOutputParser()

        # 4.调用链并流式事件返回
        for optimize_prompt in optimize_chain.stream({"prompt": prompt}):
            # 5.组装响应数据
            data = {"optimize_prompt": optimize_prompt}
            yield f"event: optimize_prompt\ndata: {json.dumps(data)}\n\n"
