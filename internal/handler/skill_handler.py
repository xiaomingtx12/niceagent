"""Creator Skill catalog HTTP endpoints."""

from dataclasses import dataclass
from uuid import UUID

from flask import request
from flask_login import current_user, login_required
from injector import inject

from internal.service import AppService, SkillService
from pkg.response import success_json


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
    def list_skills(self, app_id: UUID):
        app = self.app_service.get_app(app_id, current_user)
        rows = self.skill_service.list_skills(
            account_id=app.account_id,
            app_id=app.id,
            bindings=app.draft_app_config.skills,
            include_versions=request.args.get("include_versions") == "true",
        )
        return success_json({"items": rows})

__all__ = ["SkillHandler"]
