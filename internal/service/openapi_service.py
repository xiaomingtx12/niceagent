import json
from dataclasses import dataclass
from typing import Generator

from injector import inject

from internal.entity.app_entity import AppStatus
from internal.entity.conversation_entity import InvokeFrom, MessageStatus
from internal.exception import NotFoundException, ForbiddenException
from internal.model import Account, EndUser, Conversation, Message
from internal.schema.openapi_schema import OpenAPIChatReq
from pkg.response import Response
from pkg.sqlalchemy import SQLAlchemy
from .agent_invocation_service import AgentInvocationService, AgentInvocationSpec
from .app_config_service import AppConfigService
from .app_service import AppService
from .base_service import BaseService


@inject
@dataclass
class OpenAPIService(BaseService):
    """开放API服务"""

    db: SQLAlchemy
    app_service: AppService
    app_config_service: AppConfigService
    agent_invocation_service: AgentInvocationService

    def chat(self, req: OpenAPIChatReq, account: Account):
        """根据传递的请求+账号信息发起聊天对话，返回数据为块内容或者生成器"""
        # 1.判断当前应用是否属于当前账号
        app = self.app_service.get_app(req.app_id.data, account)

        # 2.判断当前应用是否已发布
        if app.status != AppStatus.PUBLISHED:
            raise NotFoundException("该应用不存在或未发布，请核实后重试")

        # 3.判断是否传递了终端用户id，如果传递了则检测终端用户关联的应用
        if req.end_user_id.data:
            end_user = (
                self.db.session.query(EndUser)
                .filter(
                    EndUser.id == req.end_user_id.data,
                    EndUser.app_id == app.id,
                    EndUser.tenant_id == app.account_id,
                )
                .one_or_none()
            )
            if not end_user:
                raise ForbiddenException("当前账号不存在或不属于该应用，请核实后重试")
        else:
            # 4.如果不存在则创建一个终端用户
            end_user = self.create(
                EndUser,
                **{"tenant_id": app.account_id, "app_id": app.id},
            )

        # 5.检测是否传递了会话id，如果传递了需要检测会话的归属信息
        if req.conversation_id.data:
            conversation = self.get(Conversation, req.conversation_id.data)
            if (
                not conversation
                or conversation.app_id != app.id
                or conversation.invoke_from != InvokeFrom.SERVICE_API
                or conversation.created_by != end_user.id
            ):
                raise ForbiddenException(
                    "该会话不存在，或者不属于该应用/终端用户/调用方式"
                )
        else:
            # 6.如果不存在则创建会话信息
            conversation = self.create(
                Conversation,
                **{
                    "app_id": app.id,
                    "name": "New Conversation",
                    "invoke_from": InvokeFrom.SERVICE_API,
                    "created_by": end_user.id,
                },
            )

        # 7.获取校验后的运行时配置
        app_config = self.app_config_service.get_app_config(app)

        # 8.新建一条消息记录
        message = self.create(
            Message,
            **{
                "app_id": app.id,
                "conversation_id": conversation.id,
                "invoke_from": InvokeFrom.SERVICE_API,
                "created_by": end_user.id,
                "query": req.query.data,
                "image_urls": req.image_urls.data,
                "status": MessageStatus.NORMAL,
            },
        )

        spec = AgentInvocationSpec(
            account_id=app.account_id,
            app=app,
            app_config=app_config,
            conversation_id=conversation.id,
            message_id=message.id,
            created_by=end_user.id,
            user_id=end_user.id,
            end_user_id=end_user.id,
            invoke_from=InvokeFrom.SERVICE_API,
            query=req.query.data,
            image_urls=req.image_urls.data,
            opening_statement=(
                app_config["opening_statement"] if conversation.is_new else ""
            ),
        )

        if req.stream.data is True:
            def handle_stream() -> Generator:
                for agent_thought in self.agent_invocation_service.stream(spec):
                    data = self.agent_invocation_service.serialize_event(
                        agent_thought,
                        conversation_id=conversation.id,
                        message_id=message.id,
                        end_user_id=end_user.id,
                    )
                    yield f"event: {agent_thought.event.value}\ndata:{json.dumps(data)}\n\n"

            return handle_stream()

        agent_result = self.agent_invocation_service.invoke(spec)

        return Response(
            data={
                "id": str(message.id),
                "end_user_id": str(end_user.id),
                "conversation_id": str(conversation.id),
                "query": req.query.data,
                "image_urls": req.image_urls.data,
                "answer": agent_result.answer,
                "total_token_count": agent_result.total_token_count,
                "latency": agent_result.latency,
                "agent_thoughts": [
                    {
                        "id": str(agent_thought.id),
                        "event": agent_thought.event,
                        "thought": agent_thought.thought,
                        "observation": agent_thought.observation,
                        "tool": agent_thought.tool,
                        "tool_input": agent_thought.tool_input,
                        "latency": agent_thought.latency,
                        "created_at": 0,
                    }
                    for agent_thought in agent_result.agent_thoughts
                ],
            }
        )
