"""资料读取的来源、分页与公网边界契约。"""

import io
import json
from importlib import import_module
from unittest.mock import Mock

import pytest

from internal.core.tools.builtin_tools import public_http
from internal.core.tools.builtin_tools.public_http import (
    PublicFetchError,
    PublicResponse,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "https://localhost/",
        "http://169.254.169.254/latest/meta-data/",
        "https://user:pass@example.com",
        "file:///etc/passwd",
    ],
)
def test_non_public_destinations_never_connect(monkeypatch, url):
    """拒绝内部地址和 URL 内嵌凭据，避免公网读取成为内网请求入口。"""
    monkeypatch.setattr(
        public_http.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 80))],
    )
    pool = Mock()
    monkeypatch.setattr(public_http.urllib3, "HTTPConnectionPool", pool)
    monkeypatch.setattr(public_http.urllib3, "HTTPSConnectionPool", pool)
    with pytest.raises(PublicFetchError):
        public_http.fetch_public_url(url)
    pool.assert_not_called()


def test_redirect_target_is_revalidated(monkeypatch):
    """公网服务不能通过重定向读取本机接口。"""
    dns = Mock(
        side_effect=[
            [(2, 1, 6, "", ("93.184.216.34", 80))],
            [(2, 1, 6, "", ("127.0.0.1", 80))],
        ]
    )
    monkeypatch.setattr(public_http.socket, "getaddrinfo", dns)
    response = Mock(status=302, headers={"Location": "http://127.0.0.1/admin"})
    pool = Mock()
    pool.request.return_value = response
    factory = Mock(return_value=pool)
    monkeypatch.setattr(public_http.urllib3, "HTTPConnectionPool", factory)
    with pytest.raises(PublicFetchError):
        public_http.fetch_public_url("http://example.com/")
    factory.assert_called_once()
    response.close.assert_called_once()
    pool.close.assert_called_once()


def test_https_pins_ip_retains_hostname_and_limits_body(monkeypatch):
    """连接地址固定为已验证 IP，TLS 与 Host 仍校验原始域名。"""
    monkeypatch.setattr(
        public_http.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )
    stream = io.BytesIO(b"123456")
    response = Mock(status=200, headers={"Content-Type": "text/plain"})
    response.read.side_effect = lambda size, **kwargs: stream.read(size)
    pool = Mock()
    pool.request.return_value = response
    factory = Mock(return_value=pool)
    monkeypatch.setattr(public_http.urllib3, "HTTPSConnectionPool", factory)
    with pytest.raises(PublicFetchError, match="大小限制"):
        public_http.fetch_public_url("https://example.com/doc?q=1", max_bytes=5)
    assert factory.call_args.kwargs["host"] == "93.184.216.34"
    assert factory.call_args.kwargs["assert_hostname"] == "example.com"
    assert pool.request.call_args.kwargs["headers"]["Host"] == "example.com"
    assert pool.request.call_args.kwargs["redirect"] is False
    pool.close.assert_called_once()


def test_webpage_extracts_article_sources_and_continuation(monkeypatch):
    """导航脚本不充当正文，续读链接与来源完整保留。"""
    module = import_module(
        "internal.core.tools.builtin_tools.providers.web.webpage_read"
    )
    html = (
        "<html><title>报告</title><body><nav>导航</nav><main><h1>行业分析</h1><p>"
        + "真实资料" * 600
        + '</p><a href="/source">来源</a><script>bad()</script></main></body></html>'
    )
    monkeypatch.setattr(
        module,
        "fetch_public_url",
        lambda *a, **k: PublicResponse(
            "https://example.com/report", "text/html", html.encode()
        ),
    )
    result = json.loads(
        module.webpage_read().invoke(
            {"url": "https://example.com/report", "max_chars": 1000}
        )
    )
    assert result["title"] == "报告"
    assert "导航" not in result["content"] and "bad()" not in result["content"]
    assert result["next_start"] == 1000
    assert result["links"] == [{"text": "来源", "url": "https://example.com/source"}]


def test_feed_supports_atom_and_rejects_entities(monkeypatch):
    """订阅源摘要去除 HTML，并拒绝外部实体展开。"""
    module = import_module("internal.core.tools.builtin_tools.providers.web.feed_read")
    xml = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Release</title><link href="/v2"/><summary>New version</summary><updated>2026-09-10</updated></entry></feed>'
    monkeypatch.setattr(
        module,
        "fetch_public_url",
        lambda *a, **k: PublicResponse(
            "https://example.com/feed", "application/atom+xml", xml
        ),
    )
    result = json.loads(
        module.feed_read().invoke(
            {"url": "https://example.com/feed", "keyword": "release"}
        )
    )
    assert result["items"][0]["url"] == "https://example.com/v2"
    xml = b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel><item><title>&x;</title></item></channel></rss>'
    assert "error" in json.loads(
        module.feed_read().invoke({"url": "https://example.com/feed"})
    )


def test_pdf_keeps_page_numbers_and_marks_scanned_pages(monkeypatch):
    """没有文字层的页面不能声称已经识别正文。"""
    module = import_module("internal.core.tools.builtin_tools.providers.web.pdf_read")
    monkeypatch.setattr(
        module,
        "fetch_public_url",
        lambda *a, **k: PublicResponse(
            "https://example.com/report.pdf", "application/pdf", b"%PDF-1.4"
        ),
    )
    reader = Mock(
        is_encrypted=False,
        pages=[
            Mock(extract_text=lambda: "first"),
            Mock(extract_text=lambda: ""),
            Mock(extract_text=lambda: "last"),
        ],
    )
    monkeypatch.setattr(module, "PdfReader", lambda data: reader)
    result = json.loads(
        module.pdf_read().invoke(
            {"url": "https://example.com/report.pdf", "start_page": 2, "page_count": 1}
        )
    )
    assert result["pages"] == [
        {"page": 2, "text": "", "truncated": False, "needs_ocr": True}
    ]
    assert result["next_page"] == 3
