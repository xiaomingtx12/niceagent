"""文件记忆的真实工具循环、跨会话隔离、刷新和并发回归。"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from internal.core.agent_v2.assembly.context.memory_context import (
    memory_namespace_from_runtime,
)
from internal.core.agent_v2.assembly.context.sandbox_context import (
    build_filesystem_permissions,
)
from internal.core.agent_v2.backends.memory.agent_memory_backend import (
    AgentMemoryBackend,
)
from internal.core.agent_v2.entity.agent_memory import (
    AGENT_MEMORY_INDEX,
    AgentMemoryConflict,
    AgentMemoryIdentity,
)
from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.middleware.reloading_memory_middleware import (
    ReloadingMemoryMiddleware,
)
from internal.core.agent_v2.persistence.agent_memory_repository import (
    AgentMemoryRepository,
)
from internal.core.agent_v2.execution.message_parser import message_content_to_text


def context_for(identity):
    """服务端为每个会话注入真实用户身份。"""
    return AgentRunContext(
        account_id=identity.account_id,
        app_id=identity.app_id,
        conversation_id=uuid4(),
        created_by=identity.account_id,
        principal_id=identity.account_id,
        memory_enabled=True,
    )


class FileMemoryModel(BaseChatModel):
    """按用户请求调用真正的框架工具，并检查同轮模型请求中的最新记忆。"""

    @property
    def _llm_type(self):
        """标记本地确定性模型，无网络调用。"""
        return "file-memory-test"

    def bind_tools(self, tools, **kwargs):
        """真实工具仍由图执行，只检查模型得到的工具集合。"""
        assert "write_file" in [tool.name for tool in tools]
        assert "delete_memory_file" in [tool.name for tool in tools]
        assert "update_memory" not in [tool.name for tool in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """新增、编辑和删除都由真实文件工具完成。"""
        last = messages[-1]
        path = "/memories/preferences/语言.md"
        if isinstance(last, HumanMessage):
            name, args = {
                "新增": ("write_file", {"file_path": path, "content": "默认使用中文"}),
                "修改": (
                    "edit_file",
                    {"file_path": path, "old_string": "中文", "new_string": "英文"},
                ),
                "删除": ("delete_memory_file", {"path": path}),
            }[last.content]
            message = AIMessage(
                content="",
                tool_calls=[{"name": name, "args": args, "id": str(uuid4())}],
            )
        else:
            assert isinstance(last, ToolMessage)
            assert last.status != "error", last.content
            message = AIMessage(content=message_content_to_text(messages[0].content))
        return ChatResult(generations=[ChatGeneration(message=message)])


def agent_for(store):
    """装配与项目相同的动态命名空间、原生文件权限和记忆中间件。"""
    backend = CompositeBackend(
        default=StateBackend(),
        routes={
            "/memories/": AgentMemoryBackend(
                store=store, namespace=memory_namespace_from_runtime
            ),
        },
    )
    return create_deep_agent(
        model=FileMemoryModel(),
        backend=backend,
        store=store,
        checkpointer=InMemorySaver(),
        context_schema=AgentRunContext,
        permissions=build_filesystem_permissions(memory_enabled=True),
        middleware=[
            ReloadingMemoryMiddleware(backend=backend, sources=[AGENT_MEMORY_INDEX])
        ],
    )


def test_native_tools_refresh_and_physical_delete():
    """同一线程不复用旧记忆快照，新会话共享同用户文件，其他用户不可见。"""
    store = InMemoryStore()
    identity = AgentMemoryIdentity(account_id=uuid4(), app_id=uuid4())
    context = context_for(identity)
    graph = agent_for(store)
    for query, present, absent in [
        ("新增", "默认使用中文", "默认使用英文"),
        ("修改", "默认使用英文", "默认使用中文"),
        ("删除", None, "语言.md"),
    ]:
        result = graph.invoke(
            {"messages": [HumanMessage(content=query)]},
            config=context.conversation.to_config(),
            context=context,
        )
        prompt = result["messages"][-1].content
        if present:
            assert present in prompt
        assert absent not in prompt
    assert AgentMemoryRepository(lambda: store).list_files(identity) == []
    assert store.get(identity.namespace, "/preferences/语言.md") is None
    other = AgentMemoryIdentity(account_id=uuid4(), app_id=identity.app_id)
    assert AgentMemoryRepository(lambda: store).list_files(other) == []


def test_async_native_memory_tools():
    """异步图可以使用项目配置的同步 Store 实现。"""
    store = InMemoryStore()
    context = context_for(AgentMemoryIdentity(account_id=uuid4(), app_id=uuid4()))
    result = asyncio.run(
        agent_for(store).ainvoke(
            {"messages": [HumanMessage(content="新增")]},
            config=context.conversation.to_config(),
            context=context,
        )
    )
    assert "默认使用中文" in result["messages"][-1].content


def test_atomic_save_move_delete_and_no_lost_update():
    """内容摘要只用于并发保护，不构成版本历史或恢复系统。"""
    repo = AgentMemoryRepository(lambda: store)
    store = InMemoryStore()
    identity = AgentMemoryIdentity(account_id=uuid4(), app_id=uuid4())
    row = repo.put_file(
        identity,
        path="/memories/background/项目.md",
        content="原文",
        expected_sha256="",
    )

    def save(content):
        """两个用户操作竞争同一内容摘要，只允许一个提交。"""
        try:
            return repo.put_file(
                identity,
                path=row.path,
                content=content,
                expected_sha256=row.content_sha256,
            )
        except AgentMemoryConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, ["新版甲", "新版乙"]))
    assert sum(result is not None for result in results) == 1
    current = repo.get_file(identity, row.path)
    moved = repo.put_file(
        identity,
        path="/memories/experience/测试.md",
        original_path=row.path,
        content=current.content,
        expected_sha256=current.content_sha256,
    )
    assert repo.get_file(identity, row.path) is None
    assert repo.delete_file(identity, moved.path)
    with pytest.raises(AgentMemoryConflict):
        repo.put_file(
            identity,
            path=moved.path,
            content="旧编辑器",
            expected_sha256=moved.content_sha256,
        )


def test_legacy_import_does_not_revive_deleted_files():
    """历史有效内容只导入一次；暂停和遗忘内容不会进入新目录。"""
    store = InMemoryStore()
    identity = AgentMemoryIdentity(account_id=uuid4(), app_id=uuid4())
    store.put(
        (
            "mydify",
            "agent-memory-entries",
            str(identity.account_id),
            str(identity.app_id),
        ),
        "catalog",
        {
            "entries": [
                {
                    "id": str(uuid4()),
                    "topic": "语言",
                    "category": "preference",
                    "status": status,
                    "content": status,
                    "created_at": "2026-09-09T00:00:00Z",
                    "updated_at": "2026-09-09T00:00:00Z",
                }
                for status in ["active", "paused", "forgotten", "candidate"]
            ]
        },
    )
    repo = AgentMemoryRepository(lambda: store)
    rows = repo.list_files(identity)
    assert len(rows) == 1 and rows[0].content == "active"
    repo.delete_file(identity, rows[0].path)
    assert repo.list_files(identity) == []


def test_disabled_memory_rejects_stale_checkpoint_and_index_writes():
    """关闭开关后旧状态不可读写；用户和工具都不能覆盖动态索引。"""
    from types import SimpleNamespace
    from internal.core.agent_v2.middleware.reloading_memory_middleware import (
        memory_identity,
    )

    store = InMemoryStore()
    identity = AgentMemoryIdentity(account_id=uuid4(), app_id=uuid4())
    context = context_for(identity).model_copy(update={"memory_enabled": False})
    with pytest.raises(ValueError, match="未启用"):
        memory_identity(SimpleNamespace(context=context, store=store))
    backend = AgentMemoryBackend(store=store, namespace=lambda _: identity.namespace)
    assert backend.write("/.index.md", "覆盖索引").error


def test_import_preserves_collisions_and_existing_data_over_quota():
    """升级不能因为新配额或文件重名丢弃原有内容，应用删除同时清理导入标记。"""
    store = InMemoryStore()
    identity = AgentMemoryIdentity(account_id=uuid4(), app_id=uuid4())
    repo = AgentMemoryRepository(lambda: store)
    entry_id = str(uuid4())
    for i in range(99):
        repo.put_file(identity, path=f"/memories/background/{i}.md", content="现有文件")
    path = f"/memories/preferences/语言-{entry_id[:8]}.md"
    repo.put_file(identity, path=path, content="用户手写内容")
    catalog_namespace = (
        "mydify",
        "agent-memory-entries",
        str(identity.account_id),
        str(identity.app_id),
    )
    store.put(
        catalog_namespace,
        "catalog",
        {
            "entries": [
                {
                    "id": entry_id,
                    "topic": "语言",
                    "category": "preference",
                    "status": "active",
                    "scope": "技术讨论",
                    "content": "中文",
                    "created_at": "2026-09-09T00:00:00Z",
                    "updated_at": "2026-09-09T00:00:00Z",
                }
            ]
        },
    )
    rows = repo.list_files(identity)
    assert len(rows) == 101
    assert repo.get_file(identity, path).content == "用户手写内容"
    imported = repo.get_file(identity, path.removesuffix(".md") + "-2.md")
    assert imported.content == "适用范围：技术讨论\n\n中文"
    assert store.get(catalog_namespace, "catalog") is None
    repo.delete_file(identity, imported.path)
    assert len(repo.list_files(identity)) == 100
    repo.delete_app_memories(
        account_id=str(identity.account_id), app_id=str(identity.app_id)
    )
    assert store.get(identity.namespace, "/background/0.md") is None
    assert (
        store.get(
            (
                "mydify",
                "agent-memory-files-meta",
                str(identity.account_id),
                str(identity.app_id),
            ),
            "imported",
        )
        is None
    )


def test_index_is_bounded_and_user_data_is_not_shared():
    """背景正文按需读取，其他用户的文件和偏好不进入索引。"""
    store = InMemoryStore()
    identity = AgentMemoryIdentity(account_id=uuid4(), app_id=uuid4())
    other = AgentMemoryIdentity(account_id=uuid4(), app_id=identity.app_id)
    repo = AgentMemoryRepository(lambda: store)
    repo.put_file(
        other, path="/memories/preferences/private.md", content="other-user-secret"
    )
    repo.put_file(
        identity,
        path="/memories/background/project.md",
        content="background-details-are-lazy",
    )
    backend = AgentMemoryBackend(store=store, namespace=lambda _: identity.namespace)
    index = backend.download_files(["/.index.md"])[0].content.decode()
    assert "project.md" in index
    assert "background-details-are-lazy" not in index
    assert "other-user-secret" not in index
    for i in range(90):
        repo.put_file(
            identity,
            path=f"/memories/preferences/{i}-{'长名称' * 25}.md",
            content="完整偏好" * 100,
        )
    assert len(backend.download_files(["/.index.md"])[0].content) <= 3600


def test_http_create_update_and_move_use_the_same_user_store():
    """真实 Schema 和 Handler 把空创建摘要、重命名及内容摘要传到文件用例。"""
    from types import SimpleNamespace
    from flask import Flask
    from internal.handler.memory_file_handler import MemoryFileHandler
    import internal.handler.memory_file_handler as handler_module
    from internal.service.memory_file_service import MemoryFileService

    app = Flask(__name__)
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SECRET_KEY"] = "local-memory-test"
    store = InMemoryStore()
    identity = AgentMemoryIdentity(account_id=uuid4(), app_id=uuid4())
    account = SimpleNamespace(id=identity.account_id)
    business_app = SimpleNamespace(id=identity.app_id)
    handler = MemoryFileHandler(
        app_service=SimpleNamespace(get_app=lambda app_id, user: business_app),
        web_app_service=None,
        memory_file_service=MemoryFileService(AgentMemoryRepository(lambda: store)),
        app_config_service=SimpleNamespace(
            get_draft_app_config=lambda app: {"long_term_memory": {"enable": True}}
        ),
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(handler_module, "current_user", account)
        with app.test_request_context(
            json={
                "path": "/memories/preferences/http.md",
                "content": "中文",
                "expected_sha256": "",
            },
            method="POST",
        ):
            response = handler.write_app_memory_file.__wrapped__(
                handler, identity.app_id
            )
            first = app.make_response(response).get_json()["data"]
        with app.test_request_context(
            json={
                "path": "/memories/background/http.md",
                "original_path": first["path"],
                "content": "英文",
                "expected_sha256": first["content_sha256"],
            },
            method="POST",
        ):
            moved = app.make_response(
                handler.write_app_memory_file.__wrapped__(handler, identity.app_id)
            ).get_json()["data"]
        assert moved["path"] == "/memories/background/http.md"
        assert store.get(identity.namespace, "/preferences/http.md") is None
        assert (
            store.get(identity.namespace, "/background/http.md").value["content"]
            == "英文"
        )
