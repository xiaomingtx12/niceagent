from dataclasses import dataclass

from injector import inject
from internal.core.builtin_apps import BuiltinAppManager
from internal.core.builtin_apps.entities.builtin_app_entity import BuiltinAppEntity
from internal.core.builtin_apps.entities.category_entity import CategoryEntity

from internal.entity.app_entity import AppConfigType
from internal.entity.app_entity import AppStatus
from internal.exception import NotFoundException
from internal.model import Account, App, AppConfigVersion
from internal.schema.app_config_schema import DraftAppConfigPatchReq
from pkg.sqlalchemy import SQLAlchemy
from .app_config_service import AppConfigService
from .base_service import BaseService
from .language_model_service import LanguageModelService


@inject
@dataclass
class BuiltinAppService(BaseService):
    """内置应用服务"""

    db: SQLAlchemy
    builtin_app_manager: BuiltinAppManager
    language_model_service: LanguageModelService
    app_config_service: AppConfigService

    def get_categories(self) -> list[CategoryEntity]:
        """获取分类列表信息"""
        return self.builtin_app_manager.get_categories()

    def get_builtin_apps(self) -> list[BuiltinAppEntity]:
        """获取所有内置应用实体信息列表"""
        return self.builtin_app_manager.get_builtin_apps()

    def add_builtin_app_to_space(self, builtin_app_id: str, account: Account) -> App:
        """将指定的内置应用添加到个人空间下"""
        # 1.获取内置应用信息，并检测是否存在
        builtin_app = self.builtin_app_manager.get_builtin_app(builtin_app_id)
        if not builtin_app:
            raise NotFoundException("该内置应用不存在，请核实后重试")

        # 创建前沿用编排页校验，解析当前 Skill 版本并验证子 Agent 授权范围。
        config = self.app_config_service.validate_draft_app_config(
            DraftAppConfigPatchReq.from_mapping(builtin_app.to_draft_config()),
            account_id=account.id,
        )
        model_config = self.language_model_service.get_default_model_config(account.id)

        # 2.创建自定提交上下文
        with self.db.auto_commit():
            # 3.创建应用信息
            app = App(
                account_id=account.id,
                status=AppStatus.DRAFT,
                **builtin_app.model_dump(include={"name", "icon", "description"})
            )
            self.db.session.add(app)
            self.db.session.flush()

            # 4.创建草稿配置信息
            draft_app_config = AppConfigVersion(
                app_id=app.id,
                model_config=model_config,
                config_type=AppConfigType.DRAFT,
                **config,
            )
            self.db.session.add(draft_app_config)
            self.db.session.flush()

            # 5.更新应用草稿配置
            app.draft_app_config_id = draft_app_config.id

        return app
