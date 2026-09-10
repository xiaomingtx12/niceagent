import time
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Thread
from typing import Any
from uuid import UUID, uuid4

from flask import Flask
from injector import inject
from sqlalchemy import desc
from wechatpy.exceptions import InvalidSignatureException
from wechatpy.replies import TextReply
from wechatpy.utils import check_signature

from internal.core.agent_v2.observability.run_observer import (
    PlatformAgentRunObserver,
)
from internal.core.agent_v2.execution.message_parser import message_content_to_text
from internal.entity.app_entity import AppStatus
from internal.entity.conversation_entity import MessageStatus, InvokeFrom
from internal.entity.platform_entity import WechatConfigStatus
from internal.exception import FailException
from internal.extension.logging_extension import logger
from internal.model import (
    App,
    WechatEndUser,
    EndUser,
    Message,
    WechatMessage,
    Conversation,
)
from internal.schema.agent.agent_runtime_schema import AgentInvocation
from pkg.sqlalchemy import SQLAlchemy

from internal.service.agent.agent_assembly_service import AgentAssemblyService
from internal.service.common.base_service import BaseService
from internal.service.conversation.conversation_service import ConversationService


# @deprecated 微信公众号入口已不再作为主流程使用，仅保留兼容。
@inject
@dataclass
class WechatService(BaseService):
    """[已废弃] 微信公众号旧入口，保留用于兼容已有微信配置。"""

    db: SQLAlchemy
    agent_assembly_service: AgentAssemblyService
    conversation_service: ConversationService

    def wechat(
        self,
        app_id: UUID,
        *,
        method: str,
        message: Any,
        query: Mapping[str, str | None],
        flask_app: Flask,
    ):
        """微信公众号(订阅号/服务号)校验与消息推送, 运行逻辑参考`Agent对接微信公众号思路.drawio`"""
        # 1.根据传递的app_id获取应用信息，并校验应用是否已发布
        app = self.get(App, app_id)
        msg = message
        if not app or app.status != AppStatus.PUBLISHED:
            # 2.根据不同的请求方法返回不同的数据，GET请求抛出错误、POST请求返回提示信息
            if method == "GET":
                raise FailException("该应用未发布或不存在，无法使用，请核实后重试")
            else:
                reply = TextReply(
                    content="该应用未发布或不存在，无法使用，请核实后重试", message=msg
                )
                return reply.render()

        # 3.获取应用的Wechat发布配置信息，并根据GET/POST返回不同的数据
        wechat_config = app.wechat_config
        if wechat_config.status != WechatConfigStatus.CONFIGURED:
            if method == "GET":
                raise FailException("该应用未发布到微信公众号，无法使用，请核实后重试")
            else:
                reply = TextReply(
                    content="该应用未发布到微信公众号，无法使用，请核实后重试",
                    message=msg,
                )
                return reply.render()

        # 4.校验通过，根据不同的方法执行不同的操作，GET方法校验权限，POST方法推送数据
        if method == "GET":
            # 5.从query中提取推送的签名、时间戳、nonce、echostr等数据
            signature = query.get("signature")
            timestamp = query.get("timestamp")
            nonce = query.get("nonce")
            echostr = query.get("echostr")

            # 6.校验签名信息，如果成功则原样返回echostr，否则抛出异常
            try:
                check_signature(wechat_config.wechat_token, signature, timestamp, nonce)
                return echostr
            except InvalidSignatureException:
                raise FailException("微信公众号服务器配置接入失败")
        else:
            # 7.校验发送的消息类型，仅支持传递文本消息
            if msg.type != "text":
                reply = TextReply(
                    content="抱歉，该Agent目前暂时只支持文本消息。", message=msg
                )
                return reply.render()

            # 8.获取消息内容与发送方账号(FromUserName/openid)，并查询wechat_end_user
            content = msg.content
            openid = msg.target
            wechat_end_user = (
                self.db.session.query(WechatEndUser)
                .filter(
                    WechatEndUser.openid == openid,
                    WechatEndUser.app_id == app.id,
                )
                .one_or_none()
            )

            # 9.如果wechat_end_user不存在则创建终端用户并关联记录
            if not wechat_end_user:
                with self.db.auto_commit():
                    # 10.新增终端用户并刷新获取id
                    end_user = EndUser(tenant_id=app.account_id, app_id=app.id)
                    self.db.session.add(end_user)
                    self.db.session.flush()

                    # 11.新增微信终端用户
                    wechat_end_user = WechatEndUser(
                        openid=openid,
                        app_id=app.id,
                        end_user_id=end_user.id,
                    )
                    self.db.session.add(wechat_end_user)

            # 12.判断消息的内容是否为1，并查询未推送的消息内容
            if content.strip() == "1":
                # 13.查询微信消息记录
                wechat_message = (
                    self.db.session.query(WechatMessage)
                    .filter(
                        WechatMessage.wechat_end_user_id == wechat_end_user.id,
                    )
                    .order_by(desc("created_at"))
                    .first()
                )

                # 14.检测微信消息是否存在并且未推送消息
                if wechat_message and wechat_message.is_pushed is False:
                    # 15.查询微信消息关联的Agent消息
                    message = self.get(Message, wechat_message.message_id)

                    # 16.当消息记录存在时才执行推送操作
                    if message:
                        push_content = ""
                        # 17.根据不同的消息状态执行不同的操作
                        if message.status in [MessageStatus.NORMAL, MessageStatus.STOP]:
                            # 18.单独处理答案已生成或未生成的场景
                            if message.answer.strip() != "":
                                push_content = message.answer.strip()
                                self.update(wechat_message, is_pushed=True)
                            else:
                                push_content = "该Agent智能体任务正在处理中，请稍后重新回复`1`获取结果。"
                        elif message.status == MessageStatus.TIMEOUT:
                            push_content = "该Agent智能体处理任务超时，请重新发起提问。"
                        elif message.status == MessageStatus.ERROR:
                            push_content = f"该Agent智能体处理任务出错，请重新发起提问，错误信息: {message.error}。"
                        reply = TextReply(content=push_content, message=msg)
                        return reply.render()

            # 19.消息不存在或者已推送，则将`1`作为普通输入并创建消息记录。
            conversation = (
                self.db.session.query(Conversation)
                .filter(
                    Conversation.app_id == app.id,
                    Conversation.created_by == wechat_end_user.end_user_id,
                    Conversation.invoke_from == InvokeFrom.SERVICE_API,
                    Conversation.is_deleted.is_(False),
                )
                .order_by(desc(Conversation.updated_at))
                .first()
            )
            if conversation is None:
                conversation = self.create(
                    Conversation,
                    app_id=app.id,
                    name="新的会话",
                    invoke_from=InvokeFrom.SERVICE_API,
                    created_by=wechat_end_user.end_user_id,
                )
            message = self.create(
                Message,
                **{
                    "app_id": app.id,
                    "conversation_id": conversation.id,
                    "invoke_from": InvokeFrom.SERVICE_API,
                    "created_by": wechat_end_user.end_user_id,
                    "query": content,
                    "image_urls": [],
                    "status": MessageStatus.NORMAL,
                },
            )
            self.create(
                WechatMessage,
                **{
                    "wechat_end_user_id": wechat_end_user.id,
                    "message_id": message.id,
                    "is_pushed": False,
                },
            )

            # 20.创建子线程，在子线程中运行Agent并对话
            thread = Thread(
                target=self._thread_chat,
                kwargs={
                    "flask_app": flask_app,
                    "app_id": app.id,
                    "conversation_id": conversation.id,
                    "message_id": message.id,
                    "query": content,
                },
            )
            thread.start()

            # 21.响应提示信息
            reply = TextReply(content="思考中，请回复“1”获取结果。", message=msg)
            return reply.render()

    def _thread_chat(
        self,
        flask_app: Flask,
        app_id: UUID,
        message_id: UUID,
        conversation_id: UUID,
        query: str,
    ):
        """使用子线程创建会话信息，避免数据处理超过5s"""
        with flask_app.app_context():
            app = self.get(App, app_id)
            conversation = self.get(Conversation, conversation_id)
            message = self.get(Message, message_id)
            invocation = AgentInvocation(
                account_id=app.account_id,
                app_id=app.id,
                conversation_id=conversation.id,
                message_id=message_id,
                created_by=message.created_by,
                end_user_id=message.created_by,
                invoke_from=InvokeFrom.SERVICE_API,
                query=query,
                image_urls=[],
            )
            started_at = time.monotonic()
            trace_id = uuid4()
            observer = PlatformAgentRunObserver(
                trace_id,
                attributes={
                    "account_id": str(app.account_id),
                    "app_id": str(app.id),
                    "conversation_id": str(conversation.id),
                    "invoke_from": InvokeFrom.SERVICE_API.value,
                    "channel": "wechat",
                },
            )
            try:
                agent, input_message = self.agent_assembly_service.build_agent(
                    invocation,
                    observer=observer,
                )
                # Agent 流可能持续数分钟，装配完成后先释放当前 ORM Session。
                self.db.session.close()

                state_snapshot: Mapping[str, Any] = {}
                for raw_event in agent.stream_events({"messages": [input_message]}):
                    if raw_event.get("method") != "values":
                        continue
                    params = raw_event.get("params")
                    if not isinstance(params, Mapping) or params.get("namespace"):
                        continue
                    data = params.get("data")
                    if isinstance(data, Mapping):
                        state_snapshot = data

                answer = ""
                messages = state_snapshot.get("messages")
                if isinstance(messages, list):
                    for item in reversed(messages):
                        if isinstance(item, Mapping):
                            message_type = item.get("type")
                            content = item.get("content")
                        else:
                            message_type = getattr(item, "type", None)
                            content = getattr(item, "content", None)
                        if message_type not in {"ai", "assistant"}:
                            continue
                        answer = message_content_to_text(content)
                        if answer:
                            break

                observer.finish("completed")
                updates = {
                    "status": MessageStatus.NORMAL,
                    "latency": max(0.0, time.monotonic() - started_at),
                    **ConversationService.build_run_usage_updates(
                        observer.build_run_usage()
                    ),
                }
                if answer:
                    updates["answer"] = answer
                with self.db.auto_commit():
                    persisted_message = self.db.session.get(Message, message_id)
                    if persisted_message is not None:
                        for field, value in updates.items():
                            setattr(persisted_message, field, value)
            except Exception as exc:
                observer.finish("failed", error=exc)
                with self.db.auto_commit():
                    persisted_message = self.db.session.get(Message, message_id)
                    if persisted_message is not None:
                        persisted_message.status = MessageStatus.ERROR
                        persisted_message.error = "Agent 运行失败"
                        persisted_message.latency = max(
                            0.0,
                            time.monotonic() - started_at,
                        )
                        for field, value in ConversationService.build_run_usage_updates(
                            observer.build_run_usage()
                        ).items():
                            setattr(persisted_message, field, value)
                logger.error(
                    "微信公众号 Agent 执行失败 message_id=%s",
                    message_id,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
            finally:
                self.db.session.close()
