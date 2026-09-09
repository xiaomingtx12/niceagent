import json as jsonlib
from collections.abc import Iterable
from dataclasses import field, dataclass
from typing import Any, Union, Generator

from flask import jsonify, stream_with_context, Response as FlaskResponse

from .http_code import HttpCode


@dataclass
class Response:
    """基础HTTP接口响应格式"""

    code: HttpCode = HttpCode.SUCCESS
    message: str = ""
    data: Any = field(default_factory=dict)


def json(data: Response = None):
    """基础的响应接口"""
    return jsonify(data), 200


def json_response(data: Any, status: int = 200) -> FlaskResponse:
    """返回不额外包装数据的 JSON 响应。"""
    response = jsonify(data)
    response.status_code = status
    return response


def sse_response(events: Iterable[Any]) -> FlaskResponse:
    """将事件迭代器编码为带连接和心跳注释的 SSE 响应。"""

    @stream_with_context
    def generate() -> Generator[str, None, None]:
        """连接关闭或编码失败时及时释放底层订阅，不依赖垃圾回收。"""
        iterator = iter(events)
        try:
            yield ": connected\n\n"
            for event in iterator:
                if event is None:
                    yield ": heartbeat\n\n"
                    continue
                lines: list[str] = []
                if isinstance(event, dict):
                    event_id = str(event.get("event_id") or "").replace("\r", "").replace("\n", "")
                    event_name = str(event.get("method") or "").replace("\r", "").replace("\n", "")
                    if event_id:
                        lines.append(f"id: {event_id}")
                    if event_name:
                        lines.append(f"event: {event_name}")
                lines.append(f"data: {jsonlib.dumps(event, ensure_ascii=False, separators=(',', ':'))}")
                yield "\n".join(lines) + "\n\n"
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()

    return FlaskResponse(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def success_json(data: Any = None):
    """成功数据响应"""
    return json(Response(code=HttpCode.SUCCESS, message="", data=data))


def fail_json(data: Any = None):
    """失败数据响应"""
    return json(Response(code=HttpCode.FAIL, message="", data=data))


def validate_error_json(errors: dict = None):
    """数据验证错误响应"""
    first_key = next(iter(errors))
    if first_key is not None:
        msg = errors.get(first_key)[0]
    else:
        msg = ""
    return json(Response(code=HttpCode.VALIDATE_ERROR, message=msg, data=errors))


def message(code: HttpCode = None, msg: str = ""):
    """基础的消息响应，固定返回消息提示，数据固定为空字典"""
    return json(Response(code=code, message=msg, data={}))


def success_message(msg: str = ""):
    """成功的消息响应"""
    return message(code=HttpCode.SUCCESS, msg=msg)


def fail_message(msg: str = ""):
    """失败的消息响应"""
    return message(code=HttpCode.FAIL, msg=msg)


def not_found_message(msg: str = ""):
    """未找到消息响应"""
    return message(code=HttpCode.NOT_FOUND, msg=msg)


def unauthorized_message(msg: str = ""):
    """未授权消息响应"""
    return message(code=HttpCode.UNAUTHORIZED, msg=msg)


def forbidden_message(msg: str = ""):
    """无权限消息响应"""
    return message(code=HttpCode.FORBIDDEN, msg=msg)


def too_many_requests_message(msg: str = "", retry_after: int = None):
    """请求过于频繁消息响应"""
    response = message(code=HttpCode.TOO_MANY_REQUESTS, msg=msg)
    if retry_after:
        # 添加 Retry-After 响应头
        response[0].headers["Retry-After"] = str(retry_after)
    return response


def compact_generate_response(response: Union[Response, Generator]) -> FlaskResponse:
    """兼容旧接口：生成器内容已由调用方编码时原样输出。"""
    if isinstance(response, Response):
        return json(response)

    @stream_with_context
    def generate() -> Generator:
        yield from response

    return FlaskResponse(
        generate(),
        status=200,
        mimetype="text/event-stream",
    )
