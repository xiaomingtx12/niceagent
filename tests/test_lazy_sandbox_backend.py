"""远程沙箱延迟启动的调用链与文件一致性回归测试。"""

import asyncio
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest
from deepagents.backends import FilesystemBackend
from deepagents.backends.protocol import ExecuteResponse, SandboxBackendProtocol
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from internal.core.agent_v2.assembly.context.sandbox_context import (
    SandboxContextAssembler,
    build_filesystem_permissions,
)
from internal.core.agent_v2.assembly.context.skill_context import SkillProjection
from internal.core.agent_v2.assembly.graph.deep_agent_factory import build_deep_agent
from internal.core.agent_v2.backends.composition.composite_backend_factory import (
    build_composite_backend,
    close_runtime_backends,
)
from internal.core.agent_v2.backends.sandbox import sandbox_backend_factory
from internal.core.agent_v2.backends.sandbox.managed_sandbox import close_managed_sandbox
from internal.core.agent_v2.backends.sandbox.sandbox_workspace_sync import (
    SandboxProviderError,
    seed_sandbox_workspace,
)
from internal.core.agent_v2.backends.skill_backend import SkillBackend
from internal.core.agent_v2.entity.run_context import AgentRunContext
from internal.core.agent_v2.entity.runtime_resources import AgentRuntimeResources
from internal.core.agent_v2.entity.workspace_snapshot import WorkspaceFileVersion
from internal.core.agent_v2.observability.telemetry import AgentTelemetry


class _Workspace(FilesystemBackend):
    """用真实文件操作替代外部存储，保留同步和关闭契约。"""

    def __init__(self, root):
        """单独记录资源释放，便于检查未启动和启动后的所有权。"""
        super().__init__(root_dir=root, virtual_mode=True)
        self.root = root
        self.close_count = 0

    def export_snapshot(self):
        """同步时读取最新内容，覆盖启动前写入附件的场景。"""
        files = [
            (f"/workspace/{p.relative_to(self.root).as_posix()}", p.read_bytes())
            for p in self.root.rglob("*") if p.is_file()
        ]
        return {
            path: WorkspaceFileVersion(1, sha256(content).hexdigest())
            for path, content in files
        }, files

    def upload_files(self, files, *, expected_revisions=None):
        """接受生产同步层的版本参数，文件内容仍由真实 Backend 写入。"""
        return super().upload_files([
            (path.removeprefix("/workspace") or "/", content)
            for path, content in files
        ])

    def delete_files(self, paths, *, expected_revisions=None):
        """删除同步层已确认移除的工作文件。"""
        for path in paths:
            (self.root / path.removeprefix("/workspace/")).unlink()
        return []

    def close(self):
        """记录清理次数，不删除测试需要检查的持久文件。"""
        self.close_count += 1


class _Sandbox(FilesystemBackend, SandboxBackendProtocol):
    """通过真实文件协议验证沙箱切换，无需外部账号和网络。"""

    def __init__(self, root, workspace):
        """创建沙箱文件区并保存回写目标。"""
        super().__init__(root_dir=root, virtual_mode=True)
        self.workspace = workspace
        self.baseline = None
        self.commands = []

    @property
    def id(self):
        """为测试提供稳定的资源标识。"""
        return "test-sandbox"

    def set_workspace_baseline(self, manifest):
        """接收生产播种函数生成的同步基线。"""
        self.baseline = manifest

    def execute(self, command, *, timeout=None):
        """记录执行参数，由文件工具模拟命令产物。"""
        self.commands.append((command, timeout))
        return ExecuteResponse(output="ok", exit_code=0)

    def close(self):
        """复用生产清理流程，验证文件回写而非仅检查调用次数。"""
        close_managed_sandbox(
            backend=self, workspace=self.workspace, baseline_manifest=self.baseline,
            close_provider=lambda: None, telemetry=None,
            sandbox_id=self.id, provider_name="test",
        )


@pytest.fixture(params=["daytona", "langsmith"])
def sandbox_setup(tmp_path, monkeypatch, request):
    """从生产 Context/Composite 装配入口构造可检查的运行资源。"""
    root = tmp_path / "workspace"
    root.mkdir()
    workspace = _Workspace(root)
    created = []
    skills = [("/skills/demo/SKILL.md", b"---\nname: demo\ndescription: Demo skill\n---\nHelp.")]
    skill_backend = SkillBackend([("/demo/SKILL.md", skills[0][1])])

    def create(**kwargs):
        """保留真实播种步骤，只替换 Provider 外部资源创建。"""
        remote = tmp_path / f"remote-{len(created)}"
        remote.mkdir()
        backend = _Sandbox(remote, workspace)
        created.append(backend)
        seed_sandbox_workspace(backend, workspace=workspace, skill_files=kwargs["skill_files"])
        return backend

    monkeypatch.setattr(sandbox_backend_factory, "_load_provider_creator", lambda *args: create)
    projection = SandboxContextAssembler().build(
        settings=SimpleNamespace(AGENT_SANDBOX_PROVIDER=request.param),
        workspace=workspace, sandbox_name="test-run",
        skill_projection=SkillProjection(files=skills), telemetry=AgentTelemetry(),
    )
    composite = build_composite_backend(
        routes={"/workspace/": workspace, "/skills/": skill_backend},
        sandbox=projection.backend,
    )
    yield SimpleNamespace(backend=composite, workspace=workspace, created=created)
    close_runtime_backends(composite)


def test_assembly_skills_and_attachments_do_not_start_remote_sandbox(sandbox_setup):
    """模型调用前的装配、技能读取和附件写入不能触发远程上传。"""
    setup = sandbox_setup
    assert setup.created == []
    assert setup.backend.default.id
    assert "Help." in setup.backend.read("/skills/demo/SKILL.md").file_data["content"]
    assert not setup.backend.upload_files([("/workspace/input.txt", b"attachment")])[0].error
    assert "attachment" in setup.backend.read("/workspace/input.txt").file_data["content"]
    assert setup.created == []


def test_first_execute_seeds_latest_files_and_close_persists_results(sandbox_setup):
    """启动前附件、启动后文件工具和命令应共享同一份工作文件。"""
    setup = sandbox_setup
    setup.backend.upload_files([("/workspace/input.txt", b"attachment")])
    setup.backend.execute("inspect input", timeout=7)
    assert len(setup.created) == 1
    remote = setup.created[0]
    assert remote.commands == [("inspect input", 7)]
    assert "attachment" in remote.read("/workspace/input.txt").file_data["content"]
    assert "Help." in remote.read("/skills/demo/SKILL.md").file_data["content"]
    setup.backend.write("/workspace/result.txt", "result")
    assert "result" in remote.read("/workspace/result.txt").file_data["content"]
    setup.backend.execute("inspect result")
    assert len(setup.created) == 1
    close_runtime_backends(setup.backend)
    assert (setup.workspace.root / "result.txt").read_text() == "result"
    assert setup.workspace.close_count == 1


def test_workspace_paths_remain_stable_before_and_after_execute(sandbox_setup):
    """路由切换不能改变 ls/glob/grep/下载的可见路径或编辑结果。"""
    backend = sandbox_setup.backend
    backend.write("/workspace/input.txt", "before")
    for index in range(2):
        assert backend.ls("/workspace").entries[0]["path"] == "/workspace/input.txt"
        assert backend.glob("*.txt", "/workspace").matches[0]["path"] == "/workspace/input.txt"
        assert backend.grep("before", "/workspace").matches[0]["path"] == "/workspace/input.txt"
        assert backend.download_files(["/workspace/input.txt"])[0].content == b"before"
        if index == 0:
            assert sandbox_setup.created == []
            backend.execute("start")
    assert not backend.edit("/workspace/input.txt", "before", "after").error
    assert backend.download_files(["/workspace/input.txt"])[0].content == b"after"


def test_unused_sandbox_close_does_not_create_resources(sandbox_setup):
    """普通问答完成、取消或装配失败后的清理不能隐式启动沙箱。"""
    setup = sandbox_setup
    setup.backend.ls("/")
    close_runtime_backends(setup.backend)
    close_runtime_backends(setup.backend)
    assert setup.created == []
    assert setup.workspace.close_count == 1
    with pytest.raises(SandboxProviderError, match="closed"):
        setup.backend.execute("too late")


def test_concurrent_async_tools_initialize_only_once(sandbox_setup):
    """官方异步接口会跨线程调用同步 Backend，首次创建必须合并。"""
    async def run():
        """保留附件先写入、随后并发执行的实际调用顺序。"""
        backend = sandbox_setup.backend
        await backend.aupload_files([("/workspace/input.txt", b"async")])
        assert sandbox_setup.created == []
        results = await asyncio.gather(*[backend.aexecute(f"command {i}") for i in range(4)])
        assert all(result.exit_code == 0 for result in results)

    asyncio.run(run())
    assert len(sandbox_setup.created) == 1
    assert len(sandbox_setup.created[0].commands) == 4


def test_failed_initialization_does_not_retry_closed_workspace(sandbox_setup, monkeypatch):
    """Provider 初始化失败已经释放 Workspace，后续工具不能再使用它。"""
    attempts = []

    def fail(**kwargs):
        """模拟 Provider 在清理完已接管的资源后抛出错误。"""
        attempts.append(True)
        kwargs["workspace"].close()
        raise SandboxProviderError("provider unavailable")

    monkeypatch.setattr(sandbox_backend_factory, "_load_provider_creator", lambda *args: fail)
    with pytest.raises(SandboxProviderError, match="provider unavailable"):
        sandbox_setup.backend.execute("first")
    with pytest.raises(SandboxProviderError, match="previously failed"):
        sandbox_setup.backend.read("/workspace/input.txt")
    close_runtime_backends(sandbox_setup.backend)
    assert attempts == [True]
    assert sandbox_setup.workspace.close_count == 1


class _ChatModel(FakeMessagesListChatModel):
    """给预设响应模型补齐工具绑定，运行真实 Deep Agents 图。"""

    def bind_tools(self, tools, **kwargs):
        """测试响应已包含工具调用，无需依赖外部供应商生成参数。"""
        return self


@pytest.mark.parametrize("use_execute", [False, True])
def test_real_agent_graph_initializes_sandbox_only_for_execute(sandbox_setup, use_execute):
    """覆盖技能中间件、权限和模型工具循环，确保图本身不提前启动沙箱。"""
    responses = []
    if use_execute:
        responses.append(AIMessage(content="Working.", tool_calls=[{
            "name": "execute", "args": {"command": "inspect"}, "id": "call-1",
        }]))
    responses.append(AIMessage(content="Done."))
    identity = uuid4()
    context = AgentRunContext(
        account_id=identity, app_id=uuid4(), conversation_id=uuid4(),
        created_by=identity, principal_id=identity,
    )
    graph = build_deep_agent(
        llm=_ChatModel(responses=responses), app_name="test", app_description="test",
        preset_prompt="Answer directly.", review_config={}, tools=[], subagents=[],
        runtime=AgentRuntimeResources(
            checkpointer=InMemorySaver(), backend=sandbox_setup.backend,
            permissions=build_filesystem_permissions(), skill_sources=["/skills/"],
            memory_sources=[], subagent_skill_sources={},
            runtime_context=context,
        ),
    )
    assert sandbox_setup.created == []
    result = graph.invoke(
        {"messages": [HumanMessage(content="Hello")]},
        config=context.conversation.to_config(), context=context,
    )
    assert result["messages"][-1].content == "Done."
    assert len(sandbox_setup.created) == int(use_execute)
