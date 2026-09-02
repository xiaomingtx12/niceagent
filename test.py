"""MyDify OpenAPI 调试客户端示例。

运行前设置 ``MYDIFY_API_KEY`` 和 ``MYDIFY_APP_ID``，避免把凭证写入代码或日志。
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterator, Mapping
from typing import Any

import requests


DEFAULT_API_URL = "http://localhost:5000/openapi/chat"
REQUEST_TIMEOUT = (5, 60)


def _build_request(
    *,
    api_key: str,
    app_id: str,
    query: str,
    stream: bool,
) -> tuple[dict[str, str], dict[str, Any]]:
    """构造 OpenAPI 请求头和请求体。"""
    return (
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        {"app_id": app_id, "query": query, "stream": stream},
    )


def chat_block(
    *,
    api_url: str,
    api_key: str,
    app_id: str,
    query: str,
) -> str:
    """执行非流式请求并返回完整回答。"""
    headers, payload = _build_request(
        api_key=api_key,
        app_id=app_id,
        query=query,
        stream=False,
    )
    response = requests.post(
        api_url,
        headers=headers,
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, Mapping):
        raise RuntimeError("OpenAPI 响应格式错误")
    if result.get("code") != "success":
        raise RuntimeError(result.get("message") or "OpenAPI 请求失败")
    data = result.get("data")
    return str(data.get("answer", "")) if isinstance(data, Mapping) else ""


def _stream_answers(response: requests.Response) -> Iterator[str]:
    """从 SSE 响应中提取回答增量。"""
    for line in response.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        try:
            event = json.loads(line.split(":", 1)[1].strip())
        except json.JSONDecodeError:
            continue
        answer = event.get("answer") if isinstance(event, Mapping) else None
        if answer:
            yield str(answer)


def chat_stream(
    *,
    api_url: str,
    api_key: str,
    app_id: str,
    query: str,
) -> Iterator[str]:
    """执行流式请求并逐段返回回答。"""
    headers, payload = _build_request(
        api_key=api_key,
        app_id=app_id,
        query=query,
        stream=True,
    )
    with requests.post(
        api_url,
        headers=headers,
        json=payload,
        stream=True,
        timeout=REQUEST_TIMEOUT,
    ) as response:
        response.raise_for_status()
        yield from _stream_answers(response)


def _parse_args() -> argparse.Namespace:
    """解析命令行参数和环境变量默认值。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", help="发送给 Agent 的问题")
    parser.add_argument(
        "--api-url",
        default=os.getenv("MYDIFY_API_URL", DEFAULT_API_URL),
        help="OpenAPI 地址，默认读取 MYDIFY_API_URL",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("MYDIFY_API_KEY"),
        help="API Key，默认读取 MYDIFY_API_KEY",
    )
    parser.add_argument(
        "--app-id",
        default=os.getenv("MYDIFY_APP_ID"),
        help="应用 ID，默认读取 MYDIFY_APP_ID",
    )
    parser.add_argument(
        "--block",
        action="store_true",
        help="使用非流式请求，默认使用流式请求",
    )
    args = parser.parse_args()
    if not args.api_key or not args.app_id:
        parser.error("请设置 MYDIFY_API_KEY 和 MYDIFY_APP_ID，或通过参数传入")
    return args


def main() -> None:
    """执行调试请求并输出回答。"""
    args = _parse_args()
    if args.block:
        print(
            chat_block(
                api_url=args.api_url,
                api_key=args.api_key,
                app_id=args.app_id,
                query=args.query,
            )
        )
        return

    for answer in chat_stream(
        api_url=args.api_url,
        api_key=args.api_key,
        app_id=args.app_id,
        query=args.query,
    ):
        print(answer, end="", flush=True)
    print()


if __name__ == "__main__":
    main()
