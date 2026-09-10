import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import datetime

from injector import inject
from werkzeug.datastructures import FileStorage

from internal.entity.upload_file_entity import (
    ALLOWED_IMAGE_EXTENSION,
    ALLOWED_DOCUMENT_EXTENSION,
    ALLOWED_AUDIO_EXTENSION,
    ALLOWED_VIDEO_EXTENSION,
)
from internal.exception import FailException
from internal.model import UploadFile, Account
from internal.repository import CosObjectStorageRepository, ObjectStorageError
from .upload_file_service import UploadFileService


@inject
@dataclass
class CosService:
    """腾讯云cos对象存储服务"""

    upload_file_service: UploadFileService
    object_storage: CosObjectStorageRepository

    def upload_file(
        self,
        file: FileStorage,
        only_image: bool,
        account: Account,
    ) -> UploadFile:
        """上传文件到腾讯云cos对象存储，上传后返回文件的信息"""

        # 1.提取文件扩展名并检测是否可以上传
        filename = file.filename or ""
        extension = filename.rsplit(".", 1)[-1] if "." in filename else ""
        extension_lower = extension.lower()
        if extension.lower() not in (
            ALLOWED_IMAGE_EXTENSION
            + ALLOWED_DOCUMENT_EXTENSION
            + ALLOWED_AUDIO_EXTENSION
            + ALLOWED_VIDEO_EXTENSION
        ):
            raise FailException(f"该.{extension}扩展的文件不允许上传")
        elif only_image and extension_lower not in ALLOWED_IMAGE_EXTENSION:
            raise FailException(f"该.{extension}扩展的文件不支持上传，请上传正确的图片")

        # 2.生成一个随机的名字
        random_filename = str(uuid.uuid4()) + "." + extension_lower
        now = datetime.now()
        upload_filename = f"{now.year}/{now.month:02d}/{now.day:02d}/{random_filename}"

        # 3.流式读取上传的数据并将其上传到cos中
        file_content = file.stream.read()

        try:
            # 4.通过统一 Repository 将数据上传到 COS。
            self.object_storage.put_bytes(
                upload_filename,
                file_content,
                content_type=file.mimetype,
            )
        except ObjectStorageError as exc:
            raise FailException("上传文件失败，请稍后重试") from exc

        # 5.创建 upload_file 记录。
        return self.upload_file_service.create_upload_file(
            account_id=account.id,
            name=filename,
            key=upload_filename,
            size=len(file_content),
            extension=extension_lower,
            mime_type=file.mimetype,
            hash=hashlib.sha3_256(file_content).hexdigest(),
        )

    def download_file(self, key: str, target_file_path: str):
        """下载cos云端的文件到本地的指定路径"""
        try:
            self.object_storage.download_to_file(key, target_file_path)
        except ObjectStorageError as exc:
            raise FailException("下载文件失败，请稍后重试") from exc

    def download_file_bytes(self, key: str) -> bytes:
        """读取 COS 文件原始字节，供 Agent Workspace 投影使用。"""
        try:
            return self.object_storage.get_bytes(key)
        except ObjectStorageError as exc:
            raise FailException("读取附件失败，请稍后重试") from exc

    @classmethod
    def get_file_url(cls, key: str) -> str:
        """根据传递的cos云端key获取图片的实际URL地址"""
        cos_domain = os.getenv("COS_DOMAIN")

        if not cos_domain:
            bucket = os.getenv("COS_BUCKET")
            scheme = os.getenv("COS_SCHEME")
            region = os.getenv("COS_REGION")
            cos_domain = f"{scheme}://{bucket}.cos.{region}.myqcloud.com"

        return f"{cos_domain}/{key}"
