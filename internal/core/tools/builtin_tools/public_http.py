"""公网资料读取边界：每跳校验并固定解析地址，不使用宿主代理或身份凭据。"""

from dataclasses import dataclass
import ipaddress
import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit
import requests
import urllib3


class PublicFetchError(ValueError):
    """外部资料读取失败，可直接转换为工具错误。"""


@dataclass(frozen=True)
class PublicResponse:
    """已限流、限大小的公网响应。"""

    url: str
    content_type: str
    body: bytes


def fetch_public_url(
    url: str,
    *,
    max_bytes: int = 2_000_000,
    accept: str = "text/html,application/json,application/xml,text/plain;q=0.9",
) -> PublicResponse:
    """连接校验过的 IP，同时保留 TLS 主机名，防止重定向及 DNS 重绑定访问内网。"""
    deadline = time.monotonic() + 40
    try:
        for hop in range(4):
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise PublicFetchError("只支持不含账号密码的公网 HTTP/HTTPS 地址。")
            if parsed.port not in {None, 80, 443}:
                raise PublicFetchError("只支持标准 HTTP/HTTPS 端口。")
            host = parsed.hostname.encode("idna").decode("ascii")
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            }
            if not addresses or any(
                not ipaddress.ip_address(address).is_global for address in addresses
            ):
                raise PublicFetchError("该地址不是允许读取的公网地址。")
            address = sorted(addresses)[0]
            pool_options = dict(
                host=address, port=port, timeout=urllib3.Timeout(connect=5, read=15)
            )
            pool = (
                urllib3.HTTPSConnectionPool(
                    **pool_options,
                    server_hostname=host,
                    assert_hostname=host,
                    cert_reqs="CERT_REQUIRED",
                    ca_certs=requests.certs.where(),
                )
                if parsed.scheme == "https"
                else urllib3.HTTPConnectionPool(**pool_options)
            )
            path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            host_header = f"[{host}]" if ":" in host else host
            if parsed.port:
                host_header += f":{port}"
            response = None
            try:
                if time.monotonic() > deadline:
                    raise PublicFetchError("读取超时，请稍后重试。")
                response = pool.request(
                    "GET",
                    path,
                    headers={
                        "Host": host_header,
                        "User-Agent": "NiceAgent/1.0 (public document reader)",
                        "Accept": accept,
                        "Accept-Encoding": "identity",
                    },
                    redirect=False,
                    retries=False,
                    preload_content=False,
                )
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location or hop == 3:
                        raise PublicFetchError("网页重定向次数过多或缺少目标地址。")
                    url = urljoin(url, location)
                    continue
                if response.status >= 400:
                    raise PublicFetchError(
                        f"来源服务返回 HTTP {response.status}，可能需要登录、没有权限或已达到请求额度。"
                    )
                if response.headers.get("Content-Encoding", "identity").lower() not in {
                    "identity",
                    "",
                }:
                    raise PublicFetchError(
                        "来源返回了不支持的压缩传输，请使用直接可读取的资源地址。"
                    )
                body = bytearray()
                while True:
                    if time.monotonic() > deadline:
                        raise PublicFetchError("读取超时，请稍后重试。")
                    chunk = response.read(
                        min(65536, max_bytes + 1 - len(body)), decode_content=False
                    )
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        raise PublicFetchError(
                            "资料超过本工具的大小限制，请选择较小的文档或页面。"
                        )
                return PublicResponse(
                    url=url,
                    content_type=response.headers.get("Content-Type", ""),
                    body=bytes(body),
                )
            finally:
                if response is not None:
                    response.close()
                pool.close()
    except PublicFetchError:
        raise
    except (OSError, urllib3.exceptions.HTTPError, UnicodeError, ValueError) as exc:
        raise PublicFetchError("无法读取来源，请检查公网地址和网络连接。") from exc
    raise PublicFetchError("无法完成读取。")
