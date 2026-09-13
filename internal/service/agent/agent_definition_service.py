"""应用定义读取：集中确定草稿、发布版本和恢复指纹，不创建运行资源。"""

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from injector import inject
from pydantic import ValidationError
from internal.entity.agent_invocation import AgentInvocationContext
from internal.entity.conversation_entity import InvokeFrom
from internal.exception import NotFoundException, ValidateErrorException
from internal.model import App
from internal.schema.app.app_config_schema import AgentAppConfigReq
from internal.service.app.app_config_service import AppConfigService
from pkg.sqlalchemy import SQLAlchemy


@dataclass(frozen=True)
class ApplicationAgentDefinition:
    """一次配置读取的结果，所有者和版本数据随应用定义一起传递。"""

    name: str
    description: str
    account_id: UUID
    config: AgentAppConfigReq

    def fingerprint(self) -> str:
        """只比较有效运行定义，新增的模式标记不使旧待填任务失效。"""
        payload = self.config.model_dump(
            mode="json", exclude={"subagents": {"__all__": {"model_mode"}}},
        )
        value = json.dumps([self.name, self.description, payload], sort_keys=True, ensure_ascii=True)
        return hashlib.sha256(value.encode()).hexdigest()


@inject
@dataclass
class AgentDefinitionService:
    """读取入口对应的定义；应用配置校验由 AppConfigService 执行。"""

    db: SQLAlchemy
    app_config_service: AppConfigService

    def load(
        self,
        req: AgentInvocationContext,
    ) -> ApplicationAgentDefinition:
        """读取可信入口对应的应用版本；内置助手返回代码定义，不访问 App 表。"""
        # 1.Assistant Agent 不依赖 App 配置，直接返回内置定义。
        if req.invoke_from is InvokeFrom.ASSISTANT_AGENT:
            return ApplicationAgentDefinition(
                "应用构建助手",
                "帮助 Creator 创建应用和工作流。",
                req.account_id,
                AgentAppConfigReq(),
            )

        # 2.读取并校验当前租户可访问的 App。
        app = self.db.session.get(App, req.app_id)
        if app is None:
            raise NotFoundException("应用不存在")
        try:
            # 3.Debugger 使用草稿，其余入口使用可执行的发布配置。
            if req.invoke_from is InvokeFrom.DEBUGGER:
                app_config = self.app_config_service.get_draft_agent_runtime_config(app)
            else:
                app_config = self.app_config_service.get_agent_runtime_config(app)
        except (ValidationError, ValidateErrorException) as exc:
            raise ValidateErrorException(f"应用运行配置无效: {exc}") from exc
        return ApplicationAgentDefinition(app.name, app.description, app.account_id, app_config)


    def get_definition_fingerprint(self, context: AgentInvocationContext) -> str:
        """恢复前重新读取有效定义，阻止旧图在新配置上继续运行。"""
        return self.load(context).fingerprint()
