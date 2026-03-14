from __future__ import annotations

import base64
import hashlib
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from internal.exception import ValidateErrorException


class MCPSecretCipher:
    """Encrypt/decrypt MCP sensitive values in env/headers payloads."""

    _PREFIX = "enc_v1:"
    _KEY_ENV_VARS = ("MCP_SECRET_KEY", "JWT_SECRET_KEY")

    @classmethod
    def _resolve_secret(cls) -> str:
        for env_name in cls._KEY_ENV_VARS:
            secret = os.getenv(env_name)
            if secret:
                return secret
        return ""

    @classmethod
    def _get_fernet(cls) -> Fernet:
        secret = cls._resolve_secret()
        if not secret:
            raise ValidateErrorException(
                "未配置 MCP_SECRET_KEY（或 JWT_SECRET_KEY），无法处理 MCP 敏感配置加解密"
            )
        derived_key = base64.urlsafe_b64encode(
            hashlib.sha256(secret.encode("utf-8")).digest()
        )
        return Fernet(derived_key)

    @classmethod
    def encrypt_value(cls, value: str) -> str:
        value = value or ""
        if not value:
            return value
        if value.startswith(cls._PREFIX):
            return value
        token = cls._get_fernet().encrypt(value.encode("utf-8")).decode("utf-8")
        return f"{cls._PREFIX}{token}"

    @classmethod
    def decrypt_value(cls, value: str) -> str:
        value = value or ""
        if not value:
            return value
        if not value.startswith(cls._PREFIX):
            return value
        token = value[len(cls._PREFIX) :]
        try:
            return cls._get_fernet().decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise ValidateErrorException("MCP 敏感配置解密失败，请检查密钥配置") from exc

    @classmethod
    def encrypt_env(cls, env: dict[str, str] | None) -> dict[str, str]:
        if not isinstance(env, dict):
            return {}
        return {str(key): cls.encrypt_value(str(value)) for key, value in env.items()}

    @classmethod
    def decrypt_env(cls, env: dict[str, str] | None) -> dict[str, str]:
        if not isinstance(env, dict):
            return {}
        return {str(key): cls.decrypt_value(str(value)) for key, value in env.items()}

    @classmethod
    def encrypt_headers(
        cls, headers: list[dict[str, Any]] | None
    ) -> list[dict[str, str]]:
        if not isinstance(headers, list):
            return []
        encrypted_headers: list[dict[str, str]] = []
        for header in headers:
            if not isinstance(header, dict):
                continue
            encrypted_headers.append(
                {
                    "key": str(header.get("key", "")),
                    "value": cls.encrypt_value(str(header.get("value", ""))),
                }
            )
        return encrypted_headers

    @classmethod
    def decrypt_headers(
        cls, headers: list[dict[str, Any]] | None
    ) -> list[dict[str, str]]:
        if not isinstance(headers, list):
            return []
        decrypted_headers: list[dict[str, str]] = []
        for header in headers:
            if not isinstance(header, dict):
                continue
            decrypted_headers.append(
                {
                    "key": str(header.get("key", "")),
                    "value": cls.decrypt_value(str(header.get("value", ""))),
                }
            )
        return decrypted_headers
