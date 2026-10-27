"""高德天气查询。"""

import json
import os
import requests
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from internal.lib.helper import add_attribute


class GaodeWeatherArgsSchema(BaseModel):
    """允许具体城市或行政区编码，避免同名地区歧义。"""

    city: str = Field(
        min_length=1, description="城市名称（如广州市）或六位行政区编码。"
    )


class GaodeWeatherTool(BaseTool):
    """按需访问高德 API，每次工具调用结束后释放连接。"""

    name: str = "gaode_weather"
    description: str = "查询中国城市的天气预报，包含日期和预报发布时间。"
    args_schema: type[BaseModel] = GaodeWeatherArgsSchema
    api_key: str | None = Field(default=None, exclude=True, repr=False)

    def _run(self, city: str) -> str:
        """处理空地区、服务失败和超时，不暴露请求 URL 中的凭据。"""
        key = (self.api_key or os.getenv("GAODE_API_KEY") or "").strip()
        if not key:
            return "配置错误：请配置 GAODE_API_KEY 后查询天气。"
        if not city.strip():
            return "城市不能为空。"
        try:
            with requests.Session() as session:
                response = session.get(
                    "https://restapi.amap.com/v3/config/district",
                    params={"key": key, "keywords": city.strip(), "subdistrict": 0},
                    timeout=20,
                )
                response.raise_for_status()
                data = response.json()
                if not isinstance(data, dict) or data.get("status") != "1":
                    return "城市查询失败，请检查高德凭据和配额。"
                districts = data.get("districts") or []
                if not districts:
                    return "未找到城市，请提供完整城市名或行政区编码。"
                if len(districts) != 1:
                    return "存在同名地区，请提供具体省市或行政区编码。"
                response = session.get(
                    "https://restapi.amap.com/v3/weather/weatherInfo",
                    params={
                        "key": key,
                        "city": districts[0]["adcode"],
                        "extensions": "all",
                    },
                    timeout=20,
                )
                response.raise_for_status()
                weather = response.json()
                if not isinstance(weather, dict) or weather.get("status") != "1":
                    return "天气查询失败，请稍后重试。"
                return json.dumps(weather, ensure_ascii=False)
        except requests.Timeout:
            return "天气查询超时，请稍后重试。"
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return "天气查询失败，请检查服务配置与网络。"


@add_attribute("args_schema", GaodeWeatherArgsSchema)
def gaode_weather(**kwargs) -> BaseTool:
    """保留显式传入的凭据，兼容现有工厂调用。"""
    return GaodeWeatherTool(**kwargs)
