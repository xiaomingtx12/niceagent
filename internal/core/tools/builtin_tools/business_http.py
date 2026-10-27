"""商业平台 API 的固定域名请求边界，不重试可能已成功的写操作。"""

import json
import re

import requests


class BusinessToolError(ValueError):
    """可展示的配置或平台错误，不包含请求凭据。"""


def resource_id(value: str, label: str) -> str:
    """资源标识不能改变 API 路径或查询参数。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise BusinessToolError(f"请配置有效的{label}，只填写 ID，不要填写完整网址。")
    return value


def json_object(value: str, label: str) -> dict:
    """使用 JSON 文本兼容工作流现有的标量变量协议。"""
    try:
        result = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise BusinessToolError(f"{label}必须是有效 JSON 对象。") from exc
    if not isinstance(result, dict):
        raise BusinessToolError(f"{label}必须是 JSON 对象。")
    return result


def api_request(
    platform: str, method: str, path: str, *, token: str = "", payload=None, params=None
) -> dict:
    """禁用重定向，限制响应大小；超时写入必须先核查结果以免重复创建。"""
    roots = {
        "feishu": "https://open.feishu.cn/open-apis/",
        "notion": "https://api.notion.com/v1/",
    }
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if platform == "notion":
        headers["Notion-Version"] = "2022-06-28"
    try:
        with requests.Session() as session:
            session.trust_env = False
            with session.request(
                method,
                roots[platform] + path,
                headers=headers,
                json=payload,
                params=params,
                timeout=(5, 25),
                allow_redirects=False,
                stream=True,
            ) as response:
                if not 200 <= response.status_code < 300:
                    hints = {
                        401: "凭据无效或已过期",
                        403: "缺少权限或资源未授权",
                        404: "资源不存在或未共享给集成",
                        429: "请求频率或配额受限",
                    }
                    raise BusinessToolError(
                        f"平台请求失败（HTTP {response.status_code}）：{hints.get(response.status_code, '请检查平台状态及参数')}。写入失败时请先查询结果再决定是否重试。"
                    )
                body = bytearray()
                for chunk in response.iter_content(65536):
                    body.extend(chunk)
                    if len(body) > 2_000_000:
                        raise BusinessToolError(
                            "平台响应过大，请缩小查询范围。写入操作请查询确认结果。"
                        )
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise BusinessToolError("平台返回了无法识别的响应。")
                if platform == "feishu" and data.get("code", 0) != 0:
                    code = data.get("code")
                    safe_code = code if isinstance(code, int) else "unknown"
                    raise BusinessToolError(
                        f"飞书请求失败（code={safe_code}），请核查应用权限、表格授权和字段类型。"
                    )
                return data
    except (requests.RequestException, ValueError) as exc:
        if isinstance(exc, BusinessToolError):
            raise
        raise BusinessToolError(
            "平台连接超时或响应异常；写入结果可能未知，请先查询确认，勿直接重复写入。"
        ) from exc
