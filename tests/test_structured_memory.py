"""结构化记忆生命周期、并发及实际模型工具边界回归，无外部服务。"""

import json
import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from deepagents import create_deep_agent
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.store.memory import InMemoryStore

from internal.core.agent_v2.entity.agent_memory import AgentMemoryIdentity
from internal.core.agent_v2.entity.memory_entry import (
    MemoryChange, MemoryConflict, MemorySource, estimate_memory_tokens, select_memory_entries,
)
from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.middleware.structured_memory_middleware import StructuredMemoryMiddleware
from internal.core.agent_v2.middleware.context_window_middleware import ContextWindowMiddleware
from internal.core.agent_v2.persistence.memory_entry_repository import MemoryEntryRepository
from internal.core.agent_v2.entity.agent_event import AgentEvent
from internal.service.agent_protocol.event_projector import AgentProtocolEventProjector


@pytest.fixture
def memory():
    """测试复用真正的 InMemoryStore，验证序列化后的持久状态而非内部变量。"""
    store = InMemoryStore()
    identity = AgentMemoryIdentity(account_id=uuid4(), app_id=uuid4())
    return store, identity, MemoryEntryRepository(store)


def add(repository, identity, *, topic="回答语言", content="默认使用中文", kind="user", category="preference"):
    """构造有来源的首版记录，方便测试后续不同变更路径。"""
    return repository.change(identity, MemoryChange(action="add", topic=topic, content=content, category=category),
                             MemorySource(kind=kind, evidence=content))["entry"]


def context_for(identity, *, enabled=True):
    """模拟由服务端授权的身份，不给模型传账号工具参数。"""
    return AgentRunContext(account_id=identity.account_id, app_id=identity.app_id,
                           conversation_id=uuid4(), created_by=identity.account_id,
                           principal_id=identity.account_id, memory_enabled=enabled)


def test_isolation_and_duplicate_normalization(memory):
    """同账号不同应用、同应用不同账号互不可见，标点变化不产生重复。"""
    store, identity, repository = memory
    original = add(repository, identity)
    repeated = add(repository, identity, content="默认使用中文。")
    assert repeated["id"] == original["id"]
    assert len(repository.list_entries(identity)) == 1
    assert repository.list_entries(AgentMemoryIdentity(account_id=uuid4(), app_id=identity.app_id)) == []
    assert repository.list_entries(AgentMemoryIdentity(account_id=identity.account_id, app_id=uuid4())) == []
    assert store.search(identity.namespace) == []


def test_correction_forget_restore_and_stale_version(memory):
    """修正、遗忘和恢复都创建版本；恢复不会抹掉历史，也不能覆盖新修改。"""
    _, identity, repository = memory
    entry = add(repository, identity)
    source = MemorySource(kind="user", evidence="改为英文")
    corrected = repository.change(identity, MemoryChange(action="correct", memory_id=entry["id"],
                                  expected_version=1, content="默认使用英文"), source)["entry"]
    assert corrected["version"] == 2
    with pytest.raises(MemoryConflict):
        repository.change(identity, MemoryChange(action="forget", memory_id=entry["id"], expected_version=1), source)
    forgotten = repository.change(identity, MemoryChange(action="forget", memory_id=entry["id"], expected_version=2), source)["entry"]
    assert forgotten["status"] == "forgotten"
    assert select_memory_entries(repository.list_entries(identity), "语言")[1] == ""
    # 旧内容和新内容都不能作为新增记忆自动复活。
    assert add(repository, identity)["status"] == "forgotten"
    assert add(repository, identity, content="默认使用英文")["status"] == "forgotten"
    restored = repository.change(identity, MemoryChange(action="restore", memory_id=entry["id"],
                                expected_version=3, restore_version=2), source)["entry"]
    assert restored["version"] == 4
    assert restored["content"] == "默认使用英文"
    assert [r["action"] for r in restored["revisions"]] == ["add", "correct", "forget", "restore"]


def test_concurrent_corrections_cannot_overwrite_each_other(memory):
    """多个 Repository 实例共享同一存储时只有一个旧版本写入能够成功。"""
    store, identity, repository = memory
    entry = add(repository, identity)

    def update(content):
        """模拟来自两个会话的并行修正。"""
        try:
            MemoryEntryRepository(store).change(identity, MemoryChange(
                action="correct", memory_id=entry["id"], expected_version=1, content=content,
            ), MemorySource(kind="user", evidence=content))
            return "saved"
        except MemoryConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, ["使用英文", "使用法文"])) == ["conflict", "saved"]
    assert repository.list_entries(identity)[0].version == 2


def test_agent_experience_requires_confirmation_and_paused_memory_is_not_loaded(memory):
    """成功工具经验先待确认，暂停后也不注入相关请求。"""
    _, identity, repository = memory
    entry = add(repository, identity, topic="数据库重试", content="数据库超时后重试前检查状态", kind="agent", category="experience")
    assert entry["status"] == "candidate"
    assert select_memory_entries(repository.list_entries(identity), "数据库")[1] == ""
    with pytest.raises(ValueError):
        repository.change(identity, MemoryChange(action="activate", memory_id=entry["id"], expected_version=1),
                          MemorySource(kind="agent", evidence="自我确认"))
    repository.change(identity, MemoryChange(action="activate", memory_id=entry["id"], expected_version=1),
                      MemorySource(kind="user", evidence="已核实"))
    assert "数据库超时" in select_memory_entries(repository.list_entries(identity), "数据库")[1]
    repository.change(identity, MemoryChange(action="pause", memory_id=entry["id"], expected_version=2),
                      MemorySource(kind="user", evidence="暂不使用"))
    assert select_memory_entries(repository.list_entries(identity), "数据库")[1] == ""


def test_retrieval_prefers_relevance_and_respects_whole_entry_budget(memory):
    """无关项目事实不加载，预算不足时跳过整条，不能注入缺失限定条件的半条记忆。"""
    _, identity, repository = memory
    add(repository, identity)
    add(repository, identity, topic="数据库", content="数据库使用 PostgreSQL", category="fact")
    add(repository, identity, topic="设计", content="网站采用暖灰配色", category="fact")
    selected, body = select_memory_entries(repository.list_entries(identity), "PostgreSQL 数据库", token_budget=160)
    assert selected[0].topic == "数据库"
    assert "暖灰" not in body
    assert estimate_memory_tokens(body) <= 160
    assert select_memory_entries(repository.list_entries(identity), "数据库", token_budget=1) == ([], "")


def test_same_turn_refresh_and_legacy_compatibility(memory):
    """新条目与旧文件共存，已修正主题不从旧文件注入，修改无需新建 Run。"""
    store, identity, repository = memory
    store.put(identity.namespace, "/MEMORY.md", {"content": "默认使用中文\n项目使用 uv 管理依赖", "encoding": "utf-8"})
    entry = add(repository, identity)
    request = ModelRequest(model=FakeListChatModel(responses=["ok"]), messages=[HumanMessage(content="语言")],
                           state={"messages": [HumanMessage(content="语言")]},
                           runtime=SimpleNamespace(context=context_for(identity), store=store))
    first = str(StructuredMemoryMiddleware._prepare(request).system_message.content)
    assert "项目使用 uv" in first
    repository.change(identity, MemoryChange(action="correct", memory_id=entry["id"], expected_version=1, content="默认使用英文"),
                      MemorySource(kind="user", evidence="改为英文"))
    second = str(StructuredMemoryMiddleware._prepare(request).system_message.content)
    assert "默认使用英文" in second
    assert "默认使用中文" not in second


@pytest.mark.parametrize("field", ["content", "evidence"])
def test_credentials_cannot_be_saved_in_content_or_source(field):
    """引用来源与正文使用同一凭据边界。"""
    with pytest.raises(ValueError):
        if field == "content":
            MemoryChange(action="add", topic="密码", content="password=secret")
        else:
            MemorySource(kind="user", evidence="password=secret")


def test_history_is_bounded_and_unavailable_restore_does_not_mutate(memory):
    """清理旧版本后，恢复不可用版本必须失败而不是覆盖当前状态。"""
    _, identity, repository = memory
    entry = add(repository, identity)
    for version in range(1, 24):
        repository.change(identity, MemoryChange(action="correct", memory_id=entry["id"], expected_version=version, content=f"偏好 {version}"),
                          MemorySource(kind="user", evidence="用户修正"))
    saved = repository.list_entries(identity)[0]
    assert len(saved.revisions) == 20
    with pytest.raises(ValueError):
        repository.change(identity, MemoryChange(action="restore", memory_id=entry["id"], expected_version=24, restore_version=1),
                          MemorySource(kind="user", evidence="撤销"))
    assert repository.list_entries(identity)[0] == saved


class MemoryToolModel(BaseChatModel):
    """真正运行 LangChain 工具循环，验证 ToolRuntime 身份注入和消息协议。"""

    evidence: str = "以后默认使用中文"

    @property
    def _llm_type(self):
        """本地测试模型无需 provider。"""
        return "memory-tool-test"

    def bind_tools(self, tools, **kwargs):
        """确认工具 schema 没有向模型暴露身份或 runtime。"""
        for item in tools:
            if hasattr(item, "tool_call_schema"):
                assert "runtime" not in item.tool_call_schema.model_fields
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        """工具完成后结束，避免无关的模型调用。"""
        if isinstance(messages[-1], ToolMessage):
            message = AIMessage(content="已处理")
        else:
            message = AIMessage(content="", tool_calls=[{
                "name": "update_memory", "id": "save-preference", "args": {
                    "action": "add", "evidence": self.evidence, "basis": "user_statement",
                    "topic": "回答语言", "content": "默认使用中文",
                },
            }])
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_real_agent_tool_cycle_persists_source_and_emits_receipt(memory):
    """原生工具循环确实落盘、保留来源并返回可供 UI 解析的回执。"""
    store, identity, repository = memory
    agent = create_agent(MemoryToolModel(), middleware=[StructuredMemoryMiddleware()], store=store, context_schema=AgentRunContext)
    context = context_for(identity)
    result = agent.invoke({"messages": [HumanMessage(id="input", content="以后默认使用中文")]}, context=context)
    entry = repository.list_entries(identity)[0]
    assert entry.source.conversation_id == str(context.conversation_id)
    assert entry.source.message_id == "input"
    receipt = json.loads(next(message.content for message in result["messages"] if isinstance(message, ToolMessage)))
    assert receipt["type"] == "memory_update" and receipt["changed"] is True
    assert receipt["id"] == entry.id


def test_fabricated_evidence_does_not_write(memory):
    """即使模型发出更新请求，不能伪造当前用户没有说过的话。"""
    store, identity, repository = memory
    agent = create_agent(MemoryToolModel(evidence="用户没有说过"), middleware=[StructuredMemoryMiddleware()], store=store, context_schema=AgentRunContext)
    result = agent.invoke({"messages": [HumanMessage(content="你好")]}, context=context_for(identity))
    assert repository.list_entries(identity) == []
    assert any('"error"' in message.content for message in result["messages"] if isinstance(message, ToolMessage))


def test_disabled_memory_rejects_runtime_access(memory):
    """旧工具配置被复用时也必须再次检查当前开关。"""
    store, identity, _ = memory
    request = ModelRequest(model=FakeListChatModel(responses=["ok"]), messages=[], state={"messages": []},
                           runtime=SimpleNamespace(context=context_for(identity, enabled=False), store=store))
    with pytest.raises(ValueError, match="未启用"):
        StructuredMemoryMiddleware._prepare(request)


def test_deepagents_v3_stream_publishes_memory_tool_receipt(memory):
    """验证生产使用的 DeepAgents v3 与平台公开投影不会丢弃记忆更新回执。"""
    store, identity, repository = memory
    agent = create_deep_agent(model=MemoryToolModel(), middleware=[StructuredMemoryMiddleware(), ContextWindowMiddleware()],
                              store=store, context_schema=AgentRunContext)
    projector = AgentProtocolEventProjector()
    events = []
    for raw in agent.stream_events({"messages": [HumanMessage(content="以后默认使用中文")]},
                                   context=context_for(identity), version="v3"):
        for event in projector.project_many(AgentEvent.from_raw(raw)):
            public = projector.to_public_event(event)
            if public:
                events.append(public.model_dump(mode="json"))
    receipts = [event for event in events if event["method"] == "tools"
                and event["params"]["data"].get("event") == "tool-finished"]
    assert receipts
    assert "memory_update" in json.dumps(receipts, ensure_ascii=False)
    assert repository.list_entries(identity)[0].status == "active"
    snapshots = [event["params"]["data"]["payload"] for event in events if event["method"] == "custom"]
    assert [snapshot["status"] for snapshot in snapshots] == ["running", "completed", "running", "completed"]
    memory_tokens = [next(section["tokens"] for section in snapshot["sections"] if section["key"] == "memory") for snapshot in snapshots]
    assert memory_tokens[-1] > memory_tokens[0]


def test_api_schema_projects_source_and_versions_without_operation_id(memory):
    """管理响应有完整版本与依据，但不泄露内部幂等标识。"""
    from internal.schema.memory_entry_schema import ListMemoryEntriesResp
    _, identity, repository = memory
    add(repository, identity)
    payload = ListMemoryEntriesResp().dump({"items": repository.list_entries(identity)})
    assert payload["items"][0]["source"]["evidence"] == "默认使用中文"
    assert payload["items"][0]["revisions"][0]["version"] == 1
    assert "operation_id" not in json.dumps(payload)


def test_async_agent_uses_same_identity_and_storage_boundary(memory):
    """异步请求的工作线程与同步工具必须使用同一账号应用范围。"""
    store, identity, repository = memory
    agent = create_agent(MemoryToolModel(), middleware=[StructuredMemoryMiddleware()], store=store, context_schema=AgentRunContext)
    result = asyncio.run(agent.ainvoke({"messages": [HumanMessage(content="以后默认使用中文")]}, context=context_for(identity)))
    assert result["messages"][-1].content == "已处理"
    assert len(repository.list_entries(identity)) == 1


def test_verified_experience_requires_successful_current_turn_tool(memory):
    """失败结果和旧轮次结果不能形成经验；真实当前结果也只进入候选。"""
    store, identity, repository = memory
    runtime = SimpleNamespace(context=context_for(identity), store=store, tool_call_id="experience", state={"messages": [
        HumanMessage(content="检查连接"), ToolMessage(content="已连接", tool_call_id="probe", name="probe", status="error"),
    ]})
    update = StructuredMemoryMiddleware().tools[1].func
    args = dict(runtime=runtime, action="add", evidence="已连接", basis="verified_result", topic="数据库连接", content="检查连接状态", category="experience")
    assert "error" in json.loads(update(**args))
    runtime.state["messages"][-1].status = "success"
    assert json.loads(update(**args))["status"] == "candidate"
    assert repository.list_entries(identity)[0].source.kind == "agent"


def test_tool_replay_is_idempotent_and_candidate_cannot_replace_verified_fact(memory):
    """重放不会新增版本，模型推断也不能将已确认事实改成未经确认的内容。"""
    _, identity, repository = memory
    change = MemoryChange(action="add", topic="数据库", content="使用 PostgreSQL", category="fact")
    source = MemorySource(kind="user", evidence="使用 PostgreSQL", operation_id="call-one")
    original = repository.change(identity, change, source)
    assert repository.change(identity, change, source)["changed"] is False
    with pytest.raises(ValueError, match="明确纠正"):
        repository.change(identity, MemoryChange(action="correct", memory_id=original["entry"]["id"], expected_version=1, content="使用 SQLite"),
                          MemorySource(kind="agent", evidence="SQLite 测试通过"))
    assert repository.list_entries(identity)[0].content == "使用 PostgreSQL"


def test_http_request_cannot_supply_account_identity():
    """目录作用域由鉴权结果绑定，请求体内的账号参数必须拒绝。"""
    from internal.schema.memory_entry_schema import MemoryEntryChangeReq
    with pytest.raises(ValueError):
        MemoryEntryChangeReq.model_validate({"action": "add", "topic": "语言", "content": "中文", "evidence": "用户指定", "account_id": str(uuid4())})
