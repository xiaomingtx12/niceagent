"""内部已发布 Agent 的 LangGraph SDK HTTP 适配。"""

from dataclasses import dataclass
from typing import TypeVar
from uuid import UUID

from flask import Response, request
from injector import inject
from pydantic import BaseModel, ValidationError

from internal.exception import (
    CustomException,
    ForbiddenException,
    NotFoundException,
    UnauthorizedException,
    ValidateErrorException,
)
from internal.schema.agent_protocol.internal_agent_protocol_schema import InternalRunCreateReq
from internal.entity.agent_execution_interaction import ExecutionRejected
from internal.service.internal_agent_protocol.service import (
    InternalAgentProtocolService,
)
from pkg.response import json_response


SchemaT = TypeVar("SchemaT", bound=BaseModel)


@inject
@dataclass
class InternalAgentProtocolHandler:
    """校验内部凭证并转发标准 threads/runs 请求。"""

    service: InternalAgentProtocolService

    def create_thread(self) -> Response:
        """创建 SDK 线程占位。"""
        try:
            claims = self.service.authenticate(_token_from_request())
            result = self.service.create_conversation(claims)
            return json_response(result.model_dump(mode="json"))
        except CustomException as exc:
            return _protocol_error(exc)

    def get_thread(self, conversation_id: UUID) -> Response:
        """读取线程当前状态和 values。"""
        try:
            result = self.service.get_conversation(
                claims=self.service.authenticate(_token_from_request()),
                conversation_id=conversation_id,
            )
            return json_response(result.model_dump(mode="json"))
        except CustomException as exc:
            return _protocol_error(exc)

    def create_run(self, conversation_id: UUID) -> Response:
        """创建后台 Run，并立即返回 run_id。"""
        try:
            claims = self.service.authenticate(_token_from_request())
            payload = _parse(InternalRunCreateReq)
            result = self.service.create_run(
                claims=claims,
                conversation_id=conversation_id,
                request=payload,
            )
            if isinstance(result, ExecutionRejected):
                return json_response(
                    {"detail": result.message},
                    status=409,
                )
            return json_response(result.model_dump(mode="json"), status=202)
        except CustomException as exc:
            return _protocol_error(exc)

    def get_run(self, conversation_id: UUID, run_id: UUID) -> Response:
        """读取后台 Run 的最新状态。"""
        try:
            result = self.service.get_run(
                claims=self.service.authenticate(_token_from_request()),
                conversation_id=conversation_id,
                run_id=run_id,
            )
            return json_response(result.model_dump(mode="json"))
        except CustomException as exc:
            return _protocol_error(exc)

    def cancel_run(self, conversation_id: UUID, run_id: UUID) -> Response:
        """取消指定后台 Run。"""
        try:
            self.service.cancel_run(
                claims=self.service.authenticate(_token_from_request()),
                conversation_id=conversation_id,
                run_id=run_id,
            )
            return Response(status=204)
        except CustomException as exc:
            return _protocol_error(exc)


def _token_from_request() -> str:
    """读取自定义内部头，兼容 SDK 传入的 Bearer 形式。"""
    token = request.headers.get("X-MyDify-Internal-Token")
    if token:
        return token.strip()
    parts = request.headers.get("Authorization", "").split()
    return parts[1] if len(parts) == 2 and parts[0].lower() == "bearer" else ""


def _parse(schema: type[SchemaT]) -> SchemaT:
    """将 JSON 转为边界 Schema，并统一转换校验异常。"""
    body = request.get_json(silent=True)
    try:
        return schema.model_validate(body if isinstance(body, dict) else {})
    except ValidationError as exc:
        raise ValidateErrorException(f"内部 Agent Protocol 请求格式错误: {exc}") from exc


def _protocol_error(error: CustomException) -> Response:
    """把平台业务异常转换为 LangGraph SDK 可识别的 HTTP 错误。"""
    status = next(
        (
            code
            for exception_type, code in (
                (UnauthorizedException, 401),
                (ForbiddenException, 403),
                (NotFoundException, 404),
                (ValidateErrorException, 422),
            )
            if isinstance(error, exception_type)
        ),
        500,
    )
    return json_response(
        {"detail": error.message or "内部 Agent Protocol 请求失败"},
        status=status,
    )


__all__ = ["InternalAgentProtocolHandler"]
