import io
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import requests
from injector import inject
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableParallel
from redis import Redis
from sqlalchemy import and_, func, desc, or_
from werkzeug.datastructures import FileStorage

from internal.core.tools.builtin_tools.providers.tongyi import qwen_image_generation
from internal.entity.app_entity import (
    AppStatus,
    AppConfigType,
    DEFAULT_APP_CONFIG,
    GENERATE_ICON_PROMPT_TEMPLATE,
)
from internal.entity.conversation_entity import InvokeFrom
from internal.exception import (
    NotFoundException,
    ForbiddenException,
    FailException,
    ValidateErrorException,
)
from internal.model import (
    App,
    Account,
    Conversation,
)
from internal.model.app import AppConfigVersion, AppDatasetJoin, AppConfig
from internal.schema.app.app_schema import (
    CreateAppReq,
    GetPublishHistoriesWithPageReq,
    GetAppsWithPageReq,
)
from internal.schema.app.app_config_schema import DraftAppConfigPatchReq
from pkg.paginator import Paginator
from pkg.sqlalchemy import SQLAlchemy
from .app_config_service import AppConfigService
from internal.service.agent.agent_conversation_cleanup_service import AgentConversationCleanupService
from internal.service.common.base_service import BaseService
from internal.service.file.cos_service import CosService
from internal.service.conversation.conversation_service import ConversationService
from internal.core.language_model import LanguageModelManager
from internal.entity.ai_entity import OPTIMIZE_PROMPT_TEMPLATE
from internal.lib.helper import remove_fields, generate_random_string
from internal.service.language_model.language_model_service import LanguageModelService


@inject
@dataclass
class AppService(BaseService):
    """应用服务逻辑"""

    db: SQLAlchemy
    redis_client: Redis
    cos_service: CosService
    app_config_service: AppConfigService
    language_model_manager: LanguageModelManager
    language_model_service: LanguageModelService
    agent_cleanup_service: AgentConversationCleanupService
    conversation_service: ConversationService

    def auto_create_app(self, name: str, description: str, account_id: UUID) -> None:
        """根据传递的应用名称、描述、账号id利用AI创建一个Agent智能体"""
        # 1.使用当前账号已激活的模型生成应用配置
        model_config = self.language_model_service.get_default_model_config(account_id)
        llm = self.language_model_service.load_language_model(
            model_config,
            account_id,
        )

        # 2.创建QwenImageGenerationTool包装器
        qwen_image_wrapper = qwen_image_generation(
            model="qwen-image-max", size="1024*1024"
        )

        # 3.构建生成icon链
        generate_icon_chain = (
            ChatPromptTemplate.from_template(GENERATE_ICON_PROMPT_TEMPLATE)
            | llm
            | StrOutputParser()
            | qwen_image_wrapper._run
        )

        # 4.生成预设prompt链
        generate_preset_prompt_chain = (
            ChatPromptTemplate.from_messages(
                [
                    ("system", OPTIMIZE_PROMPT_TEMPLATE),
                    ("human", "应用名称: {name}\n\n应用描述: {description}"),
                ]
            )
            | llm
            | StrOutputParser()
        )

        # 5.创建并行链同时执行两条链
        generate_app_config_chain = RunnableParallel(
            {
                "icon": generate_icon_chain,
                "preset_prompt": generate_preset_prompt_chain,
            }
        )
        app_config = generate_app_config_chain.invoke(
            {"name": name, "description": description}
        )

        # 6.将图片下载到本地后上传到腾讯云cos中
        icon_response = requests.get(app_config.get("icon"))
        if icon_response.status_code == 200:
            icon_content = icon_response.content
        else:
            raise FailException("生成应用icon图标出错")
        account = self.db.session.query(Account).get(account_id)
        upload_file = self.cos_service.upload_file(
            FileStorage(io.BytesIO(icon_content), filename="icon.png"),
            True,
            account,
        )
        icon = self.cos_service.get_file_url(upload_file.key)

        # 7.开启数据库自动提交上下文
        with self.db.auto_commit():
            # 8.创建应用记录并刷新数据，从而可以拿到应用id
            app = App(
                account_id=account.id,
                name=name,
                icon=icon,
                description=description,
            )
            self.db.session.add(app)
            self.db.session.flush()

            # 9.添加草稿记录
            app_config_version = AppConfigVersion(
                app_id=app.id,
                version=0,
                config_type=AppConfigType.DRAFT,
                **{
                    **DEFAULT_APP_CONFIG,
                    "preset_prompt": app_config.get("preset_prompt", ""),
                },
            )
            self.db.session.add(app_config_version)
            self.db.session.flush()

            # 10.更新应用配置id
            app.draft_app_config_id = app_config_version.id

    def create_app(self, req: CreateAppReq, account: Account) -> App:
        """创建Agent应用服务"""
        with self.db.auto_commit():
            # 1.创建应用记录，并刷新数据，从而可以拿到应用id
            app = App(
                account_id=account.id,
                name=req.name.data,
                icon=req.icon.data,
                description=req.description.data,
                status=AppStatus.DRAFT,
            )
            # 2.将实体类添加到session会话中
            self.db.session.add(app)
            self.db.session.flush()

            # 3.添加草稿记录
            app_config_version = AppConfigVersion(
                app_id=app.id,
                version=0,
                config_type=AppConfigType.DRAFT,
                **DEFAULT_APP_CONFIG,
            )
            self.db.session.add(app_config_version)
            self.db.session.flush()

            # 4.为应用添加草稿配置id
            app.draft_app_config_id = app_config_version.id
        return app

    def get_app(self, app_id: UUID, account: Account) -> App:
        """根据传递的id获取应用的基础信息"""
        # 1. 查询数据库获取应用基础信息
        app = self.db.session.query(App).get(app_id)

        # 2.判断应用是否存在
        if not app:
            raise NotFoundException("该应用不存在，请核实后重试")

        # 3.判断当前账号是否有权限访问该应用
        if app.account_id != account.id:
            raise ForbiddenException("当前账号无权限访问该应用，请核实后尝试")
        return app

    def get_debugger_conversation(
        self,
        *,
        app_id: UUID,
        conversation_id: UUID,
        account: Account,
    ) -> Conversation:
        """校验应用所有权和调试来源，返回已授权会话。"""
        app = self.get_app(app_id, account)
        conversation = (
            self.db.session.query(Conversation)
            .filter(
                Conversation.id == conversation_id,
                Conversation.app_id == app.id,
                Conversation.invoke_from == InvokeFrom.DEBUGGER,
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

    def delete_app(self, app_id: UUID, account: Account) -> App:
        """根据传递的应用id+账号，删除指定的应用信息，目前仅删除应用基础信息即可"""
        app = self.get_app(app_id, account)
        self.agent_cleanup_service.cleanup_app(
            account_id=account.id, app_id=app.id,
        )
        self.delete(app)
        return app

    def update_app(self, app_id: UUID, account: Account, **kwargs) -> App:
        """根据传递的应用id+账号+信息，更新指定的应用"""
        app = self.get_app(app_id, account)
        self.update(app, **kwargs)
        return app

    def copy_app(self, app_id: UUID, account: Account) -> App:
        """根据传递的应用id，拷贝Agent相关信息并创建一个新Agent"""
        # 1.获取App+草稿配置，并校验权限
        app = self.get_app(app_id, account)
        draft_app_config = app.draft_app_config

        # 2.将数据转换为字典并剔除无用数据
        app_dict = app.__dict__.copy()
        draft_app_config_dict = draft_app_config.__dict__.copy()

        # 3.剔除无用字段
        app_remove_fields = [
            "id",
            "app_config_id",
            "draft_app_config_id",
            "status",
            "updated_at",
            "created_at",
            "_sa_instance_state",
        ]
        draft_app_config_remove_fields = [
            "id",
            "app_id",
            "version",
            "updated_at",
            "created_at",
            "_sa_instance_state",
        ]
        remove_fields(app_dict, app_remove_fields)
        remove_fields(draft_app_config_dict, draft_app_config_remove_fields)
        # 4.开启数据库自动提交上下文
        with self.db.auto_commit():
            # 5.创建一个新的应用记录
            new_app = App(**app_dict, status=AppStatus.DRAFT)
            self.db.session.add(new_app)
            self.db.session.flush()

            # 6.添加草稿配置
            new_draft_app_config = AppConfigVersion(
                **draft_app_config_dict,
                app_id=new_app.id,
                version=0,
            )
            self.db.session.add(new_draft_app_config)
            self.db.session.flush()

            # 7.更新应用的草稿配置id
            new_app.draft_app_config_id = new_draft_app_config.id

        # 8.返回创建好的新应用
        return new_app

    def get_app_with_debug_conversation(
        self,
        app_id: UUID,
        account: Account,
    ) -> tuple[App, UUID]:
        """获取应用详情，并在首次打开调试工作台时创建会话。"""
        app = self.get_app(app_id, account)
        with self.db.auto_commit():
            app = (
                self.db.session.query(App)
                .populate_existing()
                .with_for_update()
                .filter(App.id == app.id)
                .one()
            )
            conversation = self.conversation_service.get_or_create_debug_conversation(
                app=app,
                account_id=account.id,
            )
        return app, conversation.id

    def get_apps_with_page(
        self, req: GetAppsWithPageReq, account: Account
    ) -> tuple[list[App], Paginator]:
        """根据传递的分页参数获取当前登录账号下的应用分页列表数据"""
        # 1.构建分页器
        paginator = Paginator(db=self.db, req=req)

        # 2.构建筛选条件
        filters = [App.account_id == account.id]
        if req.search_word.data:
            filters.append(App.name.ilike(f"%{req.search_word.data}%"))
        if req.status.data:
            filters.append(App.status == req.status.data)

        # 3.执行分页操作
        apps = paginator.paginate(
            self.db.session.query(App).filter(*filters).order_by(desc("created_at"))
        )

        return apps, paginator

    def get_published_subagent_candidates(
        self,
        app_id: UUID,
        account: Account,
        search_word: str = "",
    ) -> list[dict[str, Any]]:
        """返回当前账号下可被其他应用引用的已发布 Agent。"""
        self.get_app(app_id, account)
        query = (
            self.db.session.query(App)
            .filter(
                App.account_id == account.id,
                App.status == AppStatus.PUBLISHED,
                App.id != app_id,
            )
            .order_by(App.updated_at.desc(), App.name.asc())
        )
        if search_word.strip():
            query = query.filter(App.name.ilike(f"%{search_word.strip()}%"))

        candidates: list[dict[str, Any]] = []
        for app in query.all():
            published_config = app.app_config
            if published_config is None:
                continue
            candidates.append(
                {
                    "id": str(app.id),
                    "name": app.name,
                    "icon": app.icon,
                    "description": app.description,
                    "status": app.status,
                    "model": {
                        "provider": published_config.model_config.get(
                            "provider",
                            "",
                        ),
                        "model": published_config.model_config.get("model", ""),
                    },
                }
            )
        return candidates

    def get_draft_app_config(
        self,
        app_id: UUID,
        account: Account,
        *,
        request_base_url: str = "",
    ) -> dict[str, Any]:
        """根据传递的应用id，获取指定的应用草稿配置信息"""
        app = self.get_app(app_id, account)
        return self.app_config_service.get_draft_app_config(
            app,
            request_base_url=request_base_url,
        )

    def update_draft_app_config(
        self,
        app_id: UUID,
        draft_app_config: DraftAppConfigPatchReq,
        account: Account,
    ) -> AppConfigVersion:
        """根据传递的应用id+草稿配置修改指定应用的最新草稿"""
        # 1.获取应用信息并校验
        app = self.get_app(app_id, account)

        # 2.校验传递的草稿配置信息
        draft_app_config_record = app.draft_app_config
        draft_app_config = self.app_config_service.validate_draft_app_config(
            draft_app_config,
            account_id=account.id,
            app_id=app.id,
            current_config={
                "skills": draft_app_config_record.skills or [],
                "subagents": draft_app_config_record.subagents or [],
            },
        )

        # 3.获取当前应用的最新草稿信息
        self.update(
            draft_app_config_record,
            updated_at=datetime.now(),
            **draft_app_config,
        )

        return draft_app_config_record

    def publish_draft_app_config(self, app_id: UUID, account: Account) -> App:
        """根据传递的应用id+账号，发布/更新指定的应用草稿配置为运行时配置"""
        # 1.获取应用的信息以及草稿信息
        app = self.get_app(app_id, account)
        draft_app_config = self.get_draft_app_config(app_id, account)
        self.app_config_service.validate_published_agent_references(
            draft_app_config.get("subagents", []),
            account_id=account.id,
            app_id=app.id,
        )

        # 2.创建应用运行配置（在这里暂时不删除历史的运行配置）
        app_config = self.create(
            AppConfig,
            app_id=app_id,
            model_config=draft_app_config["model_config"],
            dialog_round=draft_app_config["dialog_round"],
            preset_prompt=draft_app_config["preset_prompt"],
            skills=draft_app_config["skills"],
            subagents=draft_app_config["subagents"],
            tools=[
                {
                    "type": tool["type"],
                    "provider_id": tool["provider"]["id"],
                    "tool_id": tool["tool"]["name"],
                    "params": tool["tool"]["params"],
                }
                for tool in draft_app_config["tools"]
            ],
            workflows=[workflow["id"] for workflow in draft_app_config["workflows"]],
            retrieval_config=draft_app_config["retrieval_config"],
            long_term_memory=draft_app_config["long_term_memory"],
            opening_statement=draft_app_config["opening_statement"],
            opening_questions=draft_app_config["opening_questions"],
            user_input=draft_app_config["user_input"],
            speech_to_text=draft_app_config["speech_to_text"],
            text_to_speech=draft_app_config["text_to_speech"],
            suggested_after_answer=draft_app_config["suggested_after_answer"],
            review_config=draft_app_config["review_config"],
        )

        # 3.更新应用关联的运行时配置以及状态
        self.update(app, app_config_id=app_config.id, status=AppStatus.PUBLISHED)

        # 4.先删除原有的知识库关联记录
        with self.db.auto_commit():
            self.db.session.query(AppDatasetJoin).filter(
                AppDatasetJoin.app_id == app_id,
            ).delete()

        # 5.新增新的知识库关联记录
        for dataset in draft_app_config["datasets"]:
            self.create(AppDatasetJoin, app_id=app_id, dataset_id=dataset["id"])

        # 6.获取应用草稿记录，并移除id、version、config_type、updated_at、created_at字段
        draft_app_config_copy = app.draft_app_config.__dict__.copy()
        remove_fields(
            draft_app_config_copy,
            [
                "id",
                "version",
                "config_type",
                "updated_at",
                "created_at",
                "_sa_instance_state",
            ],
        )

        # 7.获取当前最大的发布版本
        max_version = (
            self.db.session.query(func.coalesce(func.max(AppConfigVersion.version), 0))
            .filter(
                AppConfigVersion.app_id == app_id,
                AppConfigVersion.config_type == AppConfigType.PUBLISHED,
            )
            .scalar()
        )

        # 8.新增发布历史配置
        self.create(
            AppConfigVersion,
            version=max_version + 1,
            config_type=AppConfigType.PUBLISHED,
            **draft_app_config_copy,
        )

        return app

    def cancel_publish_app_config(self, app_id: uuid.UUID, account: Account) -> App:
        """根据传递的应用id+账号，取消发布指定的应用配置"""
        # 1.获取应用信息并校验权限
        app = self.get_app(app_id, account)

        # 2.检测下当前应用的状态是否为已发布
        if app.status != AppStatus.PUBLISHED:
            raise FailException("当前应用未发布，请核实后重试")

        # 3.修改账号的发布状态，并清空关联配置id
        self.update(app, status=AppStatus.DRAFT, app_config_id=None)

        # 4.删除应用关联的知识库信息
        with self.db.auto_commit():
            self.db.session.query(AppDatasetJoin).filter(
                AppDatasetJoin.app_id == app_id,
            ).delete()

        return app

    def get_publish_histories_with_page(
        self, app_id: UUID, req: GetPublishHistoriesWithPageReq, account: Account
    ) -> tuple[list[AppConfigVersion], Paginator]:
        """根据传递的应用id+请求数据，获取指定应用的发布历史配置列表信息"""
        # 1.获取应用信息并校验权限
        self.get_app(app_id, account)

        # 2.构建分页器
        paginator = Paginator(db=self.db, req=req)

        # 3.执行分页并获取数据
        app_config_versions = paginator.paginate(
            self.db.session.query(AppConfigVersion)
            .filter(
                AppConfigVersion.app_id == app_id,
                AppConfigVersion.config_type == AppConfigType.PUBLISHED,
            )
            .order_by(desc("version"))
        )

        return app_config_versions, paginator

    def fallback_history_to_draft(
        self,
        app_id: UUID,
        app_config_version_id: UUID,
        account: Account,
    ) -> AppConfigVersion:
        """根据传递的应用id、历史配置版本id、账号信息，回退特定配置到草稿"""
        # 1.校验应用权限并获取信息
        app = self.get_app(app_id, account)

        # 2.查询指定的历史版本配置id
        app_config_version = (
            self.db.session.query(AppConfigVersion)
            .filter(
                AppConfigVersion.id == app_config_version_id,
                AppConfigVersion.app_id == app.id,
                AppConfigVersion.config_type == AppConfigType.PUBLISHED,
            )
            .one_or_none()
        )
        if app_config_version is None:
            raise NotFoundException("该历史版本配置不存在，请核实后重试")

        # 3.校验历史版本配置信息（剔除已删除的工具、知识库、工作流）
        draft_app_config_dict = app_config_version.__dict__.copy()
        remove_fields(
            draft_app_config_dict,
            [
                "id",
                "app_id",
                "version",
                "config_type",
                "updated_at",
                "created_at",
                "_sa_instance_state",
            ],
        )
        # 4.校验历史版本配置信息
        draft_app_config_dict = self.app_config_service.validate_draft_app_config(
            DraftAppConfigPatchReq.from_mapping(draft_app_config_dict),
            account_id=account.id,
            app_id=app.id,
        )

        # 5.更新草稿配置信息
        draft_app_config_record = app.draft_app_config
        self.update(
            draft_app_config_record,
            updated_at=datetime.now(),
            **draft_app_config_dict,
        )

        return draft_app_config_record

    def get_published_config(self, app_id: UUID, account: Account) -> dict[str, Any]:
        """根据传递的应用id+账号，获取应用的发布配置"""
        # 1.获取应用信息并校验权限
        app = self.get_app(app_id, account)

        # 2.构建发布配置并返回
        return {
            "web_app": {
                "token": app.token_with_default,
                "status": app.status,
            },
            "subagent": {
                "enabled": app.status == AppStatus.PUBLISHED,
                "app_id": str(app.id),
                "name": app.name,
            },
        }

    def regenerate_web_app_token(self, app_id: UUID, account: Account) -> str:
        """根据传递的应用id+账号，重新生成WebApp凭证标识"""
        # 1.获取应用信息并校验权限
        app = self.get_app(app_id, account)

        # 2.判断应用是否已发布
        if app.status != AppStatus.PUBLISHED:
            raise FailException("应用未发布，无法生成WebApp凭证标识")

        # 3.重新生成token并更新数据
        token = generate_random_string(16)
        self.update(app, token=token)

        return token
