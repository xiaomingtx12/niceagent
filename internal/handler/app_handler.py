import uuid
from dataclasses import dataclass
from typing import Any, Dict

from flask_login import login_required
from injector import inject
from langchain.chat_models import init_chat_model
from langchain_classic.base_memory import BaseMemory
from langchain_core.runnables import RunnableConfig
from langchain_core.tracers import Run

from config.config import _get_env
from internal.exception import FailException
from internal.schema.app_schema import CompletionReq
from internal.service import AppService
from pkg.response import success_message, validate_error_json, success_json


@inject
@dataclass
class AppHandler(object):
    """应用控制器"""

    app_service: AppService

    @login_required
    def create_app(self):
        """调用服务创建新的APP记录"""
        app = self.app_service.create_app()
        return success_message(f"应用已经成功创建，id为{app.id}")

    @login_required
    def get_app(self, id: uuid.UUID):
        app = self.app_service.get_app(id)
        return success_message(f"应用已经成功获取，名字是{app.name}")

    @login_required
    def update_app(self, id: uuid.UUID):
        app = self.app_service.update_app(id)
        return success_message(f"应用已经成功修改，修改的名字是:{app.name}")

    @login_required
    def delete_app(self, id: uuid.UUID):
        app = self.app_service.delete_app(id)
        return success_message(f"应用已经成功删除，id为:{app.id}")

    @classmethod
    def _load_memory_variables(
        cls, input: Dict[str, Any], config: RunnableConfig
    ) -> Dict[str, Any]:
        """加载记忆变量信息"""
        configurable = config.get("configurable", {})
        configurable_memory = configurable.get("memory", None)
        if configurable_memory is not None and isinstance(
            configurable_memory, BaseMemory
        ):
            return configurable_memory.load_memory_variables(input)
        return {"history": []}

    @classmethod
    def _save_context(cls, run_obj: Run, config: RunnableConfig) -> None:
        """存储对应的上下文信息到记忆实体中"""
        configurable = config.get("configurable", {})
        configurable_memory = configurable.get("memory", None)
        if configurable_memory is not None and isinstance(
            configurable_memory, BaseMemory
        ):
            configurable_memory.save_context(run_obj.inputs, run_obj.outputs)

    def debug(self, app_id: uuid.UUID):
        """聊天接口"""
        # 1.提取从接口中获取的输入，POST
        req = CompletionReq()
        if not req.validate():
            return validate_error_json(req.errors)
        # 初始化模型
        # 格式：init_chat_model("提供商:模型名称")
        model = init_chat_model(
            "groq:llama-3.3-70b-versatile",  # Groq 提供的 Llama 3.3 模型
            api_key=_get_env("GROQ_API_KEY"),
        )
        # 使用实际的查询，如果请求体中有query参数
        query_text = (
            req.query.data
            if hasattr(req, "query") and req.query.data
            else "解释一下什么是递归？用一句话。"
        )
        response = model.stream(query_text)
        return success_json({"response": str(response)})

    def ping(self):
        raise FailException("数据未找到")
