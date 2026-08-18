# Agent V2 代码导读

Agent V2 的核心工作只有一件事：把平台的 AppConfig、工具、Skill、租户
运行身份和持久化资源转换为一个可执行的 DeepAgents 应用。

## 1. 从入口到图

```text
Creator / WebApp / OpenAPI / WeChat
    -> AgentRuntimeService.prepare()
    -> AgentRuntimeResources
    -> AgentConfig
    -> BaseAgent
    -> build_deep_agent()
    -> create_deep_agent()
    -> graph.stream()
```

各入口负责准备模型和工具，`runtime/configuration.py` 统一把前端/数据库
AppConfig 转成 `AgentConfig`。
它们不负责创建 Store、Workspace、Sandbox 或线程 ID。

## 2. `AgentRuntimeService`

文件：`internal/service/agent_runtime_service.py`

职责：

1. 读取当前 App 已授权 Skill；
2. 创建运行期数据库 Session；
3. 创建 `AgentRunContext`；
4. 调用 `AgentContextAssembler`；
5. 返回可交给 `BaseAgent` 的 `AgentRuntimeResources`。

它不执行模型调用，不修改图 State，也不保存业务消息。

## 3. `runtime/`

### `runtime/models.py`

定义两个运行契约：

- `AgentRunContext`：不可变的本次运行身份和 Store namespace 输入；
- `AgentRuntimeResources`：本次运行创建好的 checkpointer、Backend、Store、
  Skills 路径、Memory 路径、权限和 telemetry。

这里没有 `history`、`iteration_count` 或 `AgentRuntimePlan`。

### `runtime/assembly.py`

按固定顺序完成资源投影：

1. `WorkspaceContextAssembler` 创建 `MyDifyWorkspaceBackend`；
2. `SkillContextAssembler` 校验 Skill 子集并建立只读路由；
3. `MemoryContextAssembler` 建立 `/memories/` 的 `StoreBackend`；
4. `SandboxContextAssembler` 按配置创建官方 Sandbox；
5. `build_composite_backend()` 组合 default 和显式 routes；
6. 获取 LangGraph checkpointer；
7. 计算非 Sandbox 模式的 filesystem permissions。

这些投影对象只用于装配，不参与模型规划。

### `runtime/configuration.py`

提供 `build_agent_config_from_app_config()`，集中解释前端配置中的提示词、
审核、轮次上限和同步 SubAgent 字段。它只生成业务输入模型，不创建图、
不访问 Store，也不执行工具。

### `runtime/factory.py`

这是唯一的 DeepAgents 图工厂。它统一注册：

- `EventTranslationMiddleware`
- `RuntimeContextMiddleware`
- `ReviewMiddleware`
- LangChain 官方 retry/error middleware
- `ModelCallLimitMiddleware`
- `ToolCallLimitMiddleware`

然后传给 `create_deep_agent()`：

```python
create_deep_agent(
    model=llm,
    tools=tools,
    system_prompt=system_prompt,
    backend=backend,
    skills=skill_sources,
    memory=memory_sources,
    subagents=subagent_specs,
    checkpointer=checkpointer,
    store=store,
    permissions=permissions,
    context_schema=AgentRunContext,
)
```

DeepAgents 默认的 Todo、Filesystem、SubAgent、Summarization 和 Memory
middleware 由框架内部装配，项目不重复添加。

### 其他 runtime 模块

| 文件 | 职责 |
|---|---|
| `prompt.py` | 将 App 名称、说明、预设提示词转换成稳定 system prompt |
| `subagents.py` | 校验配置并转换成原生同步 SubAgent spec |
| `thread_config.py` | 锁定服务端 thread_id 和可信 metadata |
| `memory.py` | 创建按租户 namespace 隔离的 StoreBackend |
| `harness_profile.py` | 关闭隐式 general-purpose worker |
| `telemetry.py` | 输出不包含提示词和文件内容的结构化运行指标 |

## 4. `backends/`

### `composite.py`

只负责创建 DeepAgents 原生 `CompositeBackend`：

- default 使用 `StateBackend` 或官方 Sandbox；
- Workspace、Skill 和 Memory 使用显式路径路由；
- 不实现文件工具。

### `workspace.py`

`MyDifyWorkspaceBackend` 是 `workspace_files` 表的
`BackendProtocol` 适配器。它负责：

- 账号、App 和 Conversation 作用域；
- `/workspace/`、`/artifacts/` 虚拟路径；
- 版本化写入、配额和路径校验；
- `read/write/edit/ls/glob/grep` 等协议方法。

文件工具仍然由 DeepAgents `FilesystemMiddleware` 提供。

### `skill_catalog.py` 和 `read_only.py`

把已授权的内置 Skill 包转换为只读虚拟文件。它们不解析 Skill，也不决定
授权，只把 `SkillService` 已确认的包暴露给原生 `SkillsMiddleware`。

### `sandbox.py`

只负责官方 Sandbox provider 的生命周期：

- 稳定命名和复用；
- 上传 Workspace 初始文件和 Skill 包；
- 关闭时同步 Workspace；
- provider 失败时清理本次创建的资源。

项目不实现 `execute`、shell 或 Skill 专用执行工具。

## 5. `persistence/`

| 文件 | 职责 |
|---|---|
| `checkpoint.py` | 进程级 `PostgresSaver`、连接池和 checkpointer 配置 |
| `store.py` | 进程级 `PostgresStore`、连接池和长期文件 Store 配置 |
| `workspace.py` | `workspace_files` 版本仓储、配额和并发版本分配 |

Checkpoint 和 Store 都使用 LangGraph 原生接口，不创建平行表或自定义
状态协议。Workspace 仍保留 MyDify 业务数据需要的数据库适配。

## 6. `agents/`

### `models.py`

`AgentConfig` 是业务配置输入；`AgentThought`、`AgentResult` 和
`QueueEvent` 是 MyDify 现有 SSE/持久化协议。

`AgentTodo` 只是事件序列化模型，不是第二份 Todo State。

### `base_agent.py`

`BaseAgent` 负责：

- 接收 `AgentConfig` 和运行时资源；
- 创建 DeepAgents 图；
- 提取当前输入中的 query/image；
- 调用 `GraphExecutionDriver`；
- 将流式事件聚合为 `AgentResult`。

它不维护会话历史、不计算迭代轮次、不拼接长期记忆。

### `execution.py`

负责调用：

```python
graph.stream(
    input_state,
    config=thread_config,
    context=runtime_context,
)
```

并在本次运行结束后关闭 Workspace/Sandbox 等 Backend 资源。

### `agent_queue_manager.py`

保留 MyDify 当前的 Redis 停止标记、本地事件队列、心跳和超时协议。这是
业务 SSE 边界，不属于 DeepAgents State。

## 7. `middleware/` 和 `events/`

### `event_translation_middleware.py`

只把 LangChain 的 AIMessage、ToolMessage 和 Todo Command 转为 QueueEvent。
它不改变 DeepAgents 的控制流。

### `review_middleware.py`

执行平台已有的输入/输出关键词审核。它只读取 `state["messages"]`，没有
额外 state 字段。

### `runtime_context_middleware.py`

每次模型调用追加当前 UTC 时间。租户身份不通过提示词注入。

### `events/`

`session.py` 使用 ContextVar 管理一次图运行的事件会话，
`translator.py` 执行纯翻译，`serialization.py` 定义对外字段。

## 8. 记忆和压缩的判断方式

| 问题 | 应查看 |
|---|---|
| 会话为什么能连续 | `persistence/checkpoint.py`、`runtime/thread_config.py` |
| 长期记忆写到哪里 | `runtime/memory.py`、`AgentRunContext.memory_namespace` |
| Skill 为什么可见 | `runtime/assembly.py`、`backends/skill_catalog.py` |
| 文件为什么可见 | `backends/composite.py`、`backends/workspace.py` |
| 摘要怎么产生 | DeepAgents 原生 `SummarizationMiddleware`，不在项目代码中 |
| 为什么没有 execute | Backend 未实现 Sandbox，且 Harness Profile 关闭隐式 worker |
| SubAgent 从哪里来 | `runtime/subagents.py` 和 DeepAgents 原生 `task` |
| SSE 事件从哪里来 | `middleware/event_translation_middleware.py`、`events/` |

## 9. 当前明确不做

- 不定义 `AgentState`、`AgentRuntimePlan` 或自建 plans/todos 表；
- 不维护 `history`、`iteration_count` 或自定义上下文窗口；
- 不创建 `MemoryFact` 运行时召回/抽取链；
- 不把所有租户记忆写入同一个 `AGENTS.md`；
- 不实现第二套 filesystem、Skills、摘要、SubAgent 或 execute 工具；
- 不在未启用 Sandbox 时回退到宿主机 shell。
