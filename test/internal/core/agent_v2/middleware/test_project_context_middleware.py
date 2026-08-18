"""动态模型上下文中间件测试。"""

import asyncio
import re

from deepagents.backends import CompositeBackend
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from internal.core.agent_v2.backends.deny import DenyBackend
from internal.core.agent_v2.backends.read_only import ReadOnlyCatalogBackend
from internal.core.agent_v2.middleware.project_context_middleware import (
    ProjectContextMiddleware,
)


def _request() -> ModelRequest:
    """构造最小模型请求。"""
    return ModelRequest(
        model=FakeListChatModel(responses=["ok"]),
        messages=[HumanMessage(content="question")],
        system_message=SystemMessage(content="STABLE-L1"),
        state={},
    )


def test_wrap_model_call_only_appends_current_time():
    """同步调用仅追加非敏感动态时间。"""
    captured = {}

    def handler(updated):
        """记录修改后的请求。"""
        captured["request"] = updated
        return "done"

    result = ProjectContextMiddleware().wrap_model_call(_request(), handler)
    rendered = str(captured["request"].system_message.content)

    assert result == "done"
    assert rendered.index("STABLE-L1") < rendered.index("<运行时上下文>")
    assert re.search(r"当前时间: \d{4}-\d{2}-\d{2}T", rendered)
    assert "account" not in rendered
    assert "终端用户" not in rendered
    assert "workflow" not in rendered


def test_async_wrap_model_call_matches_sync_contract():
    """异步调用使用相同的上下文边界。"""
    captured = {}

    async def handler(updated):
        """记录异步请求。"""
        captured["request"] = updated
        return "done"

    result = asyncio.run(
        ProjectContextMiddleware().awrap_model_call(_request(), handler)
    )
    rendered = str(captured["request"].system_message.content)

    assert result == "done"
    assert "<运行时上下文>" in rendered
    assert "当前时间:" in rendered


def test_wrap_model_call_reads_context_files_through_backend():
    """模型调用从统一 Backend 读取请求级上下文文件。"""
    backend = CompositeBackend(
        default=DenyBackend(),
        routes={
            "/context/": ReadOnlyCatalogBackend(
                {"/request.md": "# Current request\n\nquestion\n"},
                missing_label="context file",
                read_only_label="Request context",
            )
        },
    )
    captured = {}

    def handler(updated):
        captured["request"] = updated
        return "done"

    result = ProjectContextMiddleware(
        backend=backend,
        sources=["/context/request.md"],
    ).wrap_model_call(_request(), handler)
    rendered = str(captured["request"].system_message.content)

    assert result == "done"
    assert "<request_context_files>" in rendered
    assert "# Current request" in rendered
    assert "question" in rendered
