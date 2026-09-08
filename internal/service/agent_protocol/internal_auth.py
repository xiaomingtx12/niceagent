"""平台内部发布 Agent 调用的短期签名凭证。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from uuid import UUID

from internal.exception import UnauthorizedException


@dataclass(frozen=True)
class InternalAgentProtocolClaims:
    """一次异步发布 Agent 调用的最小授权范围。"""

    account_id: UUID
    parent_app_id: UUID
    target_app_id: UUID
    expires_at: int


def issue_internal_agent_protocol_token(
    *,
    account_id: UUID,
    parent_app_id: UUID,
    target_app_id: UUID,
    secret: str,
    ttl_seconds: int,
    now: int | None = None,
) -> str:
    """签发绑定账号、父应用和目标应用的短期凭证。"""
    if not secret:
        raise RuntimeError("AGENT_INTERNAL_PROTOCOL_SECRET 未配置")
    issued_at = int(time.time() if now is None else now)
    expires_at = issued_at + max(60, int(ttl_seconds))
    payload = {
        "v": 1,
        "account_id": str(account_id),
        "parent_app_id": str(parent_app_id),
        "target_app_id": str(target_app_id),
        "exp": expires_at,
    }
    encoded_payload = _encode(payload)
    signature = _sign(encoded_payload, secret)
    return f"{encoded_payload}.{signature}"


def parse_internal_agent_protocol_token(
    token: str,
    *,
    secret: str,
    now: int | None = None,
) -> InternalAgentProtocolClaims:
    """校验签名和有效期，并返回严格类型化的调用范围。"""
    if not secret or not isinstance(token, str):
        raise UnauthorizedException("内部 Agent Protocol 凭证无效")
    parts = token.split(".")
    if len(parts) != 2:
        raise UnauthorizedException("内部 Agent Protocol 凭证无效")
    encoded_payload, received_signature = parts
    expected_signature = _sign(encoded_payload, secret)
    if not hmac.compare_digest(received_signature, expected_signature):
        raise UnauthorizedException("内部 Agent Protocol 凭证无效")
    try:
        payload = json.loads(_decode(encoded_payload))
    except (binascii.Error, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise UnauthorizedException("内部 Agent Protocol 凭证无效") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise UnauthorizedException("内部 Agent Protocol 凭证无效")
    try:
        expires_at = payload["exp"]
        # bool 是 int 的子类，但不能作为有效的 Unix 时间戳。
        if isinstance(expires_at, bool) or not isinstance(expires_at, int):
            raise TypeError("exp must be an integer")
        claims = InternalAgentProtocolClaims(
            account_id=UUID(str(payload["account_id"])),
            parent_app_id=UUID(str(payload["parent_app_id"])),
            target_app_id=UUID(str(payload["target_app_id"])),
            expires_at=expires_at,
        )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise UnauthorizedException("内部 Agent Protocol 凭证无效") from exc
    if claims.expires_at <= int(time.time() if now is None else now):
        raise UnauthorizedException("内部 Agent Protocol 凭证已过期")
    return claims


def _encode(value: dict[str, object]) -> str:
    """生成无填充 Base64URL JSON。"""
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode(value: str) -> str:
    """解码无填充 Base64URL JSON。"""
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}").decode()


def _sign(value: str, secret: str) -> str:
    """对规范化 payload 计算 HMAC-SHA256。"""
    digest = hmac.new(secret.encode(), value.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


__all__ = [
    "InternalAgentProtocolClaims",
    "issue_internal_agent_protocol_token",
    "parse_internal_agent_protocol_token",
]
