"""按需读取 RSS/Atom 订阅源中的最新条目。"""

import json
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from internal.lib.helper import add_attribute
from internal.core.tools.builtin_tools.public_http import (
    fetch_public_url,
    PublicFetchError,
)


class FeedReadArgs(BaseModel):
    """从用户提供的实际订阅源读取更新，不虚构热榜。"""

    url: str = Field(
        max_length=2048,
        description="RSS 或 Atom 订阅源地址，例如博客、产品更新日志、新闻源。",
    )
    limit: int = Field(
        default=10, ge=1, le=30, description="最多返回条目数量，保持订阅源原顺序。"
    )
    keyword: str = Field(
        default="", max_length=200, description="可选关键词，按标题和摘要过滤。"
    )


class FeedReadTool(BaseTool):
    """返回更新标题、日期、原文链接和摘要，用于每日资料整理。"""

    name: str = "feed_read"
    description: str = (
        "读取 RSS/Atom 最新文章、产品发布和行业动态，可按关键词筛选；这是一次读取，不会自动创建定时监控。"
    )
    args_schema: type[BaseModel] = FeedReadArgs

    def _run(self, url: str, limit: int = 10, keyword: str = "") -> str:
        """使用禁止外部实体的 XML 解析器，条目正文只作为资料处理。"""
        try:
            response = fetch_public_url(
                url,
                accept="application/rss+xml,application/atom+xml,application/xml,text/xml",
            )
            root = ET.fromstring(response.body)
            ns = {"a": "http://www.w3.org/2005/Atom"}
            atom = root.tag == "{http://www.w3.org/2005/Atom}feed"
            if not atom and root.tag != "rss":
                raise PublicFetchError("该地址不是 RSS 2.0 或 Atom 订阅源。")
            entries = (
                root.findall("a:entry", ns) if atom else root.findall("./channel/item")
            )
            items = []
            for entry in entries:
                title = entry.findtext(
                    "a:title" if atom else "title", default="", namespaces=ns
                )
                summary = entry.findtext(
                    "a:summary" if atom else "description", default="", namespaces=ns
                )
                if atom and not summary:
                    summary = entry.findtext("a:content", default="", namespaces=ns)
                summary = BeautifulSoup(summary, "html.parser").get_text(
                    " ", strip=True
                )
                if keyword.casefold() not in (title + " " + summary).casefold():
                    continue
                if atom:
                    link = next(
                        (
                            node.get("href", "")
                            for node in entry.findall("a:link", ns)
                            if node.get("rel", "alternate") == "alternate"
                        ),
                        "",
                    )
                    published = entry.findtext(
                        "a:published", default="", namespaces=ns
                    ) or entry.findtext("a:updated", default="", namespaces=ns)
                else:
                    link = entry.findtext("link", default="")
                    published = entry.findtext("pubDate", default="")
                items.append(
                    {
                        "title": title,
                        "url": urljoin(response.url, link) if link else "",
                        "published": published,
                        "summary": summary[:2000],
                    }
                )
                if len(items) >= limit:
                    break
            return json.dumps(
                {"source_url": response.url, "items": items}, ensure_ascii=False
            )
        except (PublicFetchError, ET.ParseError, DefusedXmlException) as exc:
            return json.dumps(
                {
                    "error": (
                        str(exc)
                        if isinstance(exc, PublicFetchError)
                        else "订阅源 XML 无效。"
                    )
                },
                ensure_ascii=False,
            )


@add_attribute("args_schema", FeedReadArgs)
def feed_read(**kwargs) -> BaseTool:
    """返回订阅源读取工具。"""
    return FeedReadTool()
