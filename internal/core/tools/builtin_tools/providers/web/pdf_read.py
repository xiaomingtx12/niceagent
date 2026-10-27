"""从公网 PDF 按页提取可引用的原文。"""

import io
import json
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from internal.lib.helper import add_attribute
from internal.core.tools.builtin_tools.public_http import (
    fetch_public_url,
    PublicFetchError,
)


class PdfReadArgs(BaseModel):
    """按页读取论文或报告，保留来源与页码。"""

    url: str = Field(max_length=2048, description="公开 PDF 文件的直链，最大 8 MB。")
    start_page: int = Field(
        default=1, ge=1, le=5000, description="起始页码，从 1 开始。"
    )
    page_count: int = Field(
        default=3, ge=1, le=8, description="本次读取页数，最多 8 页。"
    )


class PdfReadTool(BaseTool):
    """返回分页面文本，扫描件明确标记为无法直接提取。"""

    name: str = "pdf_read"
    description: str = (
        "读取在线论文、政策文件和行业报告 PDF 的指定页面，返回原文、页码和后续页码，方便引用核查。"
    )
    args_schema: type[BaseModel] = PdfReadArgs

    def _run(self, url: str, start_page: int = 1, page_count: int = 3) -> str:
        """仅处理直接可解析的 PDF，不尝试绕过加密或伪造 OCR 文本。"""
        try:
            response = fetch_public_url(
                url, max_bytes=8_000_000, accept="application/pdf"
            )
            if not response.body.lstrip().startswith(b"%PDF-"):
                raise PublicFetchError("地址未返回 PDF 文件，请提供 PDF 直链。")
            reader = PdfReader(io.BytesIO(response.body))
            if reader.is_encrypted:
                raise PublicFetchError("PDF 已加密，请提供可直接读取的版本。")
            total = len(reader.pages)
            if start_page > total:
                raise PublicFetchError(f"起始页超出范围，文档共 {total} 页。")
            end = min(total, start_page + page_count - 1)
            pages = []
            for number in range(start_page, end + 1):
                text = reader.pages[number - 1].extract_text() or ""
                pages.append(
                    {
                        "page": number,
                        "text": text[:12000],
                        "truncated": len(text) > 12000,
                        "needs_ocr": not text.strip(),
                    }
                )
            return json.dumps(
                {
                    "url": response.url,
                    "total_pages": total,
                    "pages": pages,
                    "next_page": end + 1 if end < total else None,
                },
                ensure_ascii=False,
            )
        except (PublicFetchError, PdfReadError, ValueError, RecursionError) as exc:
            return json.dumps(
                {
                    "error": (
                        str(exc)
                        if isinstance(exc, PublicFetchError)
                        else "PDF 无法解析，请尝试其他来源或上传到文档处理流程。"
                    )
                },
                ensure_ascii=False,
            )


@add_attribute("args_schema", PdfReadArgs)
def pdf_read(**kwargs) -> BaseTool:
    """返回在线 PDF 原文读取工具。"""
    return PdfReadTool()
