"""显式时区的时间查询，避免容器时区影响业务结果。"""

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from internal.lib.helper import add_attribute


class CurrentTimeArgsSchema(BaseModel):
    """时区属于本次查询输入，默认使用中国标准时间。"""

    timezone: str = Field(
        default="Asia/Shanghai",
        description="IANA 时区，例如 Asia/Shanghai、UTC、America/New_York",
    )


class CurrentTimeTool(BaseTool):
    """返回含 UTC 偏移的当前时间。"""

    name: str = "current_time"
    description: str = "查询指定时区当前时间，返回 ISO 8601 时间及 UTC 偏移。"
    args_schema: type[BaseModel] = CurrentTimeArgsSchema

    def _run(self, timezone: str = "Asia/Shanghai") -> str:
        """未知时区返回可修正的提示，不回退为宿主机时间。"""
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return "不支持的时区，请使用 Asia/Shanghai、UTC 等 IANA 时区名称。"
        return f"{datetime.now(zone).isoformat(timespec='seconds')} [{timezone}]"


@add_attribute("args_schema", CurrentTimeArgsSchema)
def current_time(**kwargs) -> BaseTool:
    """通过统一工厂提供参数描述和运行实例。"""
    return CurrentTimeTool()
