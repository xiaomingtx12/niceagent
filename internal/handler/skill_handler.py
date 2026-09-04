"""内置 Skill Catalog HTTP 接口。"""

from dataclasses import dataclass
from uuid import UUID

from flask_login import current_user, login_required
from injector import inject

from internal.schema.skill_schema import (
    BuiltinSkillDetailResp,
    ListBuiltinSkillsResp,
)
from internal.service import AppService, SkillService
from pkg.response import success_json


@inject
@dataclass
class SkillHandler:
    """校验访问权限并序列化内置 Skill 接口响应。"""

    app_service: AppService
    skill_service: SkillService

    @login_required
    def list_catalog(self):
        """返回平台内置 Skill 模板目录。"""
        return success_json(
            ListBuiltinSkillsResp().dump(
                {"items": self.skill_service.list_catalog()}
            )
        )

    @login_required
    def get_catalog_skill(self, skill_name: str):
        """返回单个 Skill 模板及其 SKILL.md 内容。"""
        skill = self.skill_service.get_catalog_skill(skill_name)
        return success_json(BuiltinSkillDetailResp().dump(skill))

    @login_required
    def list_skills(self, app_id: UUID):
        """返回当前用户可访问应用的 Skill 目录及启用状态。"""
        app = self.app_service.get_app(app_id, current_user)
        skills = self.skill_service.list_catalog(
            app.draft_app_config.skills,
        )
        return success_json(
            ListBuiltinSkillsResp().dump({"items": skills})
        )

__all__ = ["SkillHandler"]
