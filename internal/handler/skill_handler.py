"""Creator Skill catalog HTTP endpoints."""

from dataclasses import dataclass
from uuid import UUID

from flask import request
from flask_login import current_user, login_required
from injector import inject

from internal.schema.skill_schema import GetSkillsReq
from internal.service import AppService, SkillService
from pkg.response import success_json, validate_error_json


@inject
@dataclass
class SkillHandler:
    app_service: AppService
    skill_service: SkillService

    @login_required
    def list_catalog(self):
        """返回平台内置 Skill 模板目录。"""
        return success_json(
            {
                "items": [
                    skill.to_dict(enabled=True)
                    for skill in self.skill_service.list_catalog()
                ]
            }
        )

    @login_required
    def get_catalog_skill(self, skill_name: str):
        """返回单个 Skill 模板及其 SKILL.md 内容。"""
        skill = self.skill_service.get_catalog_skill(skill_name)
        return success_json({**skill.to_dict(enabled=True), "content": skill.skill_md})

    @login_required
    def list_skills(self, app_id: UUID):
        req = GetSkillsReq(request.args)
        if not req.validate():
            return validate_error_json(req.errors)

        app = self.app_service.get_app(app_id, current_user)
        rows = self.skill_service.list_skills(
            account_id=app.account_id,
            app_id=app.id,
            bindings=app.draft_app_config.skills,
            include_versions=req.include_versions.data,
        )
        return success_json({"items": rows})

__all__ = ["SkillHandler"]
