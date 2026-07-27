"""memory_facts HTTP handler (R4 end-user "我的记忆" 5 端点 + creator 1 端点).

业务接口层 (HTTP):
- WebApp token 鉴权 (继承 master 模式 — 用 current_user 作 end-user 标识):
  - GET    /web-apps/<token>/memory/facts          list (end-user 本人 + account)
  - POST   /web-apps/<token>/memory/facts          create (手动写入, end_user_id 来自会话)
  - GET    /web-apps/<token>/memory/facts/<fact_id> read one (校验可见性)
  - DELETE /web-apps/<token>/memory/facts/<fact_id> soft_delete (end-user 限本人)
- Creator @login_required:
  - GET    /apps/<app_id>/memory/facts             list_creator (全 app, 可过滤)
  - DELETE /apps/<app_id>/memory/facts/<fact_id>   soft_delete (creator 全权)

end-user 鉴权现状: master 用 login_required + Account (即 creator 本人);
真正 anonymous end-user 鉴权待 R5+ 接入 — 本轮按 master 模式保持一致,
end_user_id 默认取 current_user.id (creator 登录时), 这与 plan §3.1 语义
"creator 同时也能用 WebApp 自己玩" 一致.

约束 (硬约束 #5):
- 复用 master handler dataclass + inject + pkg.response 模式
- 业务逻辑全在 MemoryFactService; handler 只做参数解析 + 鉴权 + 序列化
"""
from dataclasses import dataclass
from uuid import UUID

from flask import request
from flask_login import login_required, current_user
from injector import inject

from internal.schema.memory_fact_schema import (
    CreateMemoryFactReq,
    GetMemoryFactsReq,
)
from internal.service import MemoryFactService, WebAppService, AppService
from internal.model import App
from pkg.response import (
    success_json,
    validate_error_json,
    success_message,
)


@inject
@dataclass
class MemoryFactHandler:
    """memory_facts 控制器."""

    memory_fact_service: MemoryFactService
    web_app_service: WebAppService  # for token → app 解析 (复用 master 模式)
    app_service: AppService  # for app_id → app (creator 鉴权)

    # ---------- WebApp token 鉴权端点 ----------

    @login_required
    def list_my_memory_facts(self, token: str):
        """GET /web-apps/<token>/memory/facts - 端用户 '我的记忆' 列表."""
        app = self._resolve_web_app(token)
        # master 模式: end_user_id = current_user.id (creator 当 WebApp 用户);
        # 真 anonymous end-user 鉴权待 R5+
        end_user_id = str(current_user.id)
        facts = self.memory_fact_service.list_end_user_facts(
            account_id=str(app.account_id),
            app_id=str(app.id),
            end_user_id=end_user_id,
        )
        return success_json({"items": [f.to_dict() for f in facts], "total": len(facts)})

    @login_required
    def create_my_memory_fact(self, token: str):
        """POST /web-apps/<token>/memory/facts - 端用户手动写入."""
        req = CreateMemoryFactReq()
        if not req.validate():
            return validate_error_json(req.errors)
        app = self._resolve_web_app(token)
        end_user_id = str(current_user.id)
        fact = self.memory_fact_service.create_fact(
            account_id=str(app.account_id),
            app_id=str(app.id),
            end_user_id=end_user_id,
            fact_type=req.fact_type.data,
            content=req.content.data,
            confidence=req.confidence.data or 1.0,
            auto_extracted=False,
        )
        return success_json(fact.to_dict())

    @login_required
    def get_my_memory_fact(self, token: str, fact_id: UUID):
        """GET /web-apps/<token>/memory/facts/<fact_id> - 单个 fact."""
        app = self._resolve_web_app(token)
        end_user_id = str(current_user.id)
        facts = self.memory_fact_service.list_end_user_facts(
            account_id=str(app.account_id),
            app_id=str(app.id),
            end_user_id=end_user_id,
        )
        for f in facts:
            if f.id == fact_id:
                return success_json(f.to_dict())
        return validate_error_json({"fact_id": "fact 不存在或无权访问"})

    @login_required
    def delete_my_memory_fact(self, token: str, fact_id: UUID):
        """DELETE /web-apps/<token>/memory/facts/<fact_id> - 端用户软删除本人."""
        app = self._resolve_web_app(token)
        end_user_id = str(current_user.id)
        self.memory_fact_service.soft_delete_fact(
            account_id=str(app.account_id),
            fact_id=fact_id,
            actor_end_user_id=end_user_id,
            actor_is_creator=False,
        )
        return success_message("删除成功")

    # ---------- Creator 端点 ----------

    @login_required
    def list_app_memory_facts(self, app_id: UUID):
        """GET /apps/<app_id>/memory/facts - creator 面板列表."""
        req = GetMemoryFactsReq()
        if not req.validate():
            return validate_error_json(req.errors)
        # AppService.get_app 已经做 account_id 校验 (NotFound / Forbidden)
        app = self.app_service.get_app(app_id, current_user)
        facts = self.memory_fact_service.list_creator_facts(
            account_id=str(app.account_id),
            app_id=str(app.id),
            end_user_id_filter=req.end_user_id.data,
            fact_type_filter=req.fact_type.data,
        )
        return success_json({"items": [f.to_dict() for f in facts], "total": len(facts)})

    @login_required
    def delete_app_memory_fact(self, app_id: UUID, fact_id: UUID):
        """DELETE /apps/<app_id>/memory/facts/<fact_id> - creator 软删除."""
        # AppService.get_app 已经做 account_id 校验
        app = self.app_service.get_app(app_id, current_user)
        self.memory_fact_service.soft_delete_fact(
            account_id=str(app.account_id),
            fact_id=fact_id,
            actor_is_creator=True,
        )
        return success_message("删除成功")

    # ---------- helpers ----------

    def _resolve_web_app(self, token: str) -> App:
        """复用 WebAppService.get_web_app 按 token 拿 app + 校验发布状态."""
        return self.web_app_service.get_web_app(token)


__all__ = ["MemoryFactHandler"]