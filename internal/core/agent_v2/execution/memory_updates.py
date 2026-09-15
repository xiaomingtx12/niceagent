"""文件提交后发布轻量变更事件，不公开记忆正文。"""

from typing import Literal

from langgraph.config import get_stream_writer

from internal.core.agent_v2.entity.stream_event import CustomEventType
from internal.extension.logging_extension import logger


def publish_memory_update(
    *, path: str, action: Literal["write", "delete"]
) -> None:
    """只在图执行中发布；管理接口和独立文件上传通过正常响应确认成功。"""
    logger.debug("agent_memory_write outcome=committed operation=%s", action)
    try:
        writer = get_stream_writer()
    except RuntimeError:
        return
    writer({
        "type": CustomEventType.MEMORY_FILE_UPDATE,
        "path": path,
        "action": action,
    })
