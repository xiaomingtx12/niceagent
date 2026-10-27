"""腾讯云 COS 对象存储适配器。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from injector import inject
from qcloud_cos import CosConfig, CosS3Client

from config import Config


class ObjectStorageError(RuntimeError):
    """COS 操作失败时抛出，避免基础设施异常泄露到业务边界。"""


@inject
@dataclass
class CosObjectStorageRepository:
    """提供与业务模型无关的 COS 字节读写能力。"""

    config: Config
    _client_instance: CosS3Client | None = field(default=None, init=False, repr=False)

    def put_bytes(
        self,
        key: str,
        content: bytes,
        *,
        content_type: str | None = None,
    ) -> None:
        """写入完整对象。"""
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": key,
            "Body": bytes(content),
        }
        if content_type:
            kwargs["ContentType"] = content_type
        try:
            self._client.put_object(**kwargs)
        except Exception as exc:  # COS SDK 暴露多种 provider 异常
            raise ObjectStorageError("COS 对象上传失败") from exc

    def get_bytes(self, key: str, *, max_bytes: int | None = None) -> bytes:
        """读取对象字节；预览场景可限制读取范围。"""
        body: Any = None
        try:
            kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
            if max_bytes is not None:
                if max_bytes <= 0:
                    raise ValueError("max_bytes 必须大于 0")
                kwargs["Range"] = f"bytes=0-{max_bytes - 1}"
            response = self._client.get_object(**kwargs)
            body = response["Body"] if isinstance(response, dict) else response
            if hasattr(body, "get_raw_stream"):
                body = body.get_raw_stream()
            content = body.read() if hasattr(body, "read") else body
        except Exception as exc:
            raise ObjectStorageError("COS 对象读取失败") from exc
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise ObjectStorageError("COS 返回了无效的对象内容")
        return bytes(content)

    def delete(self, key: str) -> None:
        """删除对象；删除不存在对象由 COS 按幂等语义处理。"""
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            raise ObjectStorageError("COS 对象删除失败") from exc

    def download_to_file(self, key: str, target_file_path: str) -> None:
        """把对象下载到调用方指定的本地路径。"""
        try:
            self._client.download_file(
                Bucket=self._bucket,
                Key=key,
                DestFilePath=target_file_path,
            )
        except Exception as exc:
            raise ObjectStorageError("COS 对象下载失败") from exc

    def get_download_url(self, key: str, *, expires_seconds: int = 300) -> str:
        """生成短期私有下载地址。"""
        if expires_seconds <= 0:
            raise ValueError("expires_seconds 必须大于 0")
        try:
            return self._client.get_presigned_url(
                Method="GET",
                Bucket=self._bucket,
                Key=key,
                Expired=expires_seconds,
            )
        except Exception as exc:
            raise ObjectStorageError("COS 下载地址生成失败") from exc

    @property
    def _bucket(self) -> str:
        bucket = str(self.config.COS_BUCKET or "").strip()
        if not bucket:
            raise ObjectStorageError("COS_BUCKET 未配置")
        return bucket

    @property
    def _client(self) -> CosS3Client:
        if self._client_instance is None:
            if not self.config.COS_SECRET_ID or not self.config.COS_SECRET_KEY:
                raise ObjectStorageError("COS 访问凭据未配置")
            cos_config = CosConfig(
                Region=self.config.COS_REGION,
                SecretId=self.config.COS_SECRET_ID,
                SecretKey=self.config.COS_SECRET_KEY,
                Token=None,
                Scheme=self.config.COS_SCHEME,
            )
            self._client_instance = CosS3Client(cos_config)
        return self._client_instance


__all__ = ["CosObjectStorageRepository", "ObjectStorageError"]
