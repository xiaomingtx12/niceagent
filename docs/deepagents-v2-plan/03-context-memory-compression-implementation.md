# 上下文、记忆与压缩实现

本文只描述当前 Agent V2 的运行方案。核心原则是：

```text
DeepAgents 原生 State / Memory / Summarization
    + LangGraph Checkpoint / Store
    + MyDify 租户、Workspace 和事件适配
```

项目不维护第二套历史、摘要、token 裁剪或事实记忆运行链。

## 1. 三种概念

| 概念 | 载体 | 生命周期 | 用途 |
|---|---|---|---|
| 图 State | DeepAgents 原生 State | 当前 thread 的 checkpoint | messages、Todo、filesystem 和 middleware 状态 |
| Runtime Context | `AgentRunContext` | 当前调用 | 租户、App、Conversation、用户和权限作用域 |
| 长期文件记忆 | StoreBackend 下的 `/memories/AGENTS.md` | 跨 thread | 用户偏好、稳定事实和长期约束 |

不要把三者混成一个 `AgentState`。

## 2. 图 State

Agent V2 不定义自有 `state_schema`。`create_deep_agent()` 的原生 State
已经包含框架需要的 `messages` 和内置 middleware 状态。

项目不再把以下字段创建或传递到 DeepAgents State：

- `history`
- `iteration_count`
- `task_id`（业务队列/轨迹可以保留独立的运行标识）
- `account_id`
- `app_id`
- `conversation_id`

调用轮次限制不是状态字段，而是：

```python
ModelCallLimitMiddleware(run_limit=max_model_calls)
ToolCallLimitMiddleware(run_limit=max_tool_calls)
```

Todo 使用 DeepAgents 原生 `write_todos`，其状态由 checkpoint 保存；业务层
只在事件翻译和持久化时创建 `AgentTodo` DTO。

## 3. Runtime Context

`AgentRunContext` 是 `context_schema`：

```python
create_deep_agent(
    ...,
    context_schema=AgentRunContext,
)
```

执行时：

```python
graph.stream(
    {"messages": messages},
    config=thread_config,
    context=runtime_context,
)
```

Runtime Context 用于：

- 构造稳定的 `thread_id`；
- 计算 Store namespace；
- 让业务工具读取可信的租户身份；
- 让 middleware 和 Backend adapter 获取当前运行作用域；
- 输出脱敏 telemetry。

Runtime Context 不会自动注入提示词。只有工具或 middleware 明确读取并使用
它时，模型才会看到相应结果。

## 4. Checkpoint 和会话记忆

### 4.1 Thread ID

线程 ID 由服务端生成，不能由客户端覆盖：

```text
(account_id,app_id,conversation_id,agent_id)
```

同一 Conversation 必须复用同一 thread ID；不同租户、App、Conversation
或 Agent 路由必须使用不同 ID。

### 4.2 Checkpointer

`persistence/checkpoint.py` 只负责 LangGraph Saver 生命周期：

- 生产：`PostgresSaver`；
- 开发/离线：`InMemorySaver`；
- 关闭：进程退出时释放连接池。

它不解析消息、不拼接 history、不执行摘要。

### 4.3 消息输入

入口每次只把当前请求消息作为新的 `messages` 输入。连续会话的历史由
checkpointer 恢复，DeepAgents 的 messages reducer 负责合并。

原始 `Message` 表继续保存业务问答、展示和审计数据，但不作为每次请求的
手工 history 字段传给 Agent。

## 5. 长期文件记忆

### 5.1 原生装配

启用 Store 时：

```python
CompositeBackend(
    default=StateBackend(),
    routes={
        "/memories/": StoreBackend(
            namespace=memory_namespace_from_runtime,
        ),
    },
)

create_deep_agent(
    backend=backend,
    store=store,
    memory=["/memories/AGENTS.md"],
)
```

`StoreBackend` 使用 LangGraph 的 `BaseStore`。项目没有 `MemoryFact` 模型、
运行时召回服务或自动事实抽取器。

### 5.2 多租户隔离

namespace 来自服务端 `AgentRunContext`：

```python
(
    "mydify",
    str(account_id),
    str(app_id),
    str(end_user_id or "account"),
)
```

所有租户都可以有一个逻辑路径 `/memories/AGENTS.md`，但 namespace 不同，
所以它们是不同的 Store item。不会把所有租户写入同一个文件。

作用域含义：

| 调用主体 | namespace 末段 | 记忆范围 |
|---|---|---|
| Creator/Account | `account` | Account + App |
| EndUser | EndUser UUID | Account + App + EndUser |

### 5.3 写入边界

原生 MemoryMiddleware 默认允许模型用 `edit_file` 更新记忆文件，因此非
Sandbox 权限允许 `/memories/**` 写入，同时禁止 Skill 目录写入。
StoreBackend 自己负责文件读写；模型不能修改 namespace 或越过 `/memories/`
路由。

如果业务后续需要“只读记忆”或管理员审核，应在配置层使用权限策略或
HITL，不要新增第二套 Memory Backend。

## 6. Skills 和 Memory 的区别

| 项目 | Skills | Memory |
|---|---|---|
| 路径 | `/skills/`、`/subagent_skills/` | `/memories/` |
| 加载方式 | 原生 SkillsMiddleware 渐进式加载 | 原生 MemoryMiddleware 启动时加载 |
| 内容 | 操作流程和领域知识 | 用户偏好、长期事实和约束 |
| 默认写入 | 禁止 | 允许按权限更新 |
| 数据来源 | 平台内置 Skill 包 | LangGraph Store |

Skill 不应被当作记忆，记忆也不应通过 Skill frontmatter 注入。

## 7. Backend 和文件上下文

### 7.1 业务文件

```text
/workspace/        -> MyDifyWorkspaceBackend
/artifacts/        -> MyDifyWorkspaceBackend
```

Workspace Backend 负责版本、配额、路径和数据库作用域。模型只看到虚拟
路径，不看到 account/app/conversation 物理存储字段。

### 7.2 框架内部文件

DeepAgents 会按需使用内部外置文件。它们留在默认 StateBackend 或 Sandbox
中，由框架自己的 Filesystem/Summarization middleware 管理。项目不将它们
写入业务 Workspace 表，也不把它们作为 API `history` 字段。

### 7.3 Sandbox

启用官方 Sandbox 时，Sandbox 是 CompositeBackend 的 default：

- `execute` 和默认文件工具使用同一个隔离文件系统；
- Workspace/Artifacts 在开始时同步到 Sandbox；
- 结束时只把 Workspace/Artifacts 同步回业务 Backend；
- Skill 包可以上传到 Sandbox 供 Skill 脚本使用；
- provider 失败时不回退到宿主机 shell。

未启用 Sandbox 时，Backend 不实现 `SandboxBackendProtocol`，原生
FilesystemMiddleware 不会暴露 `execute`。

## 8. 压缩策略

压缩完全由 DeepAgents 原生 middleware 完成：

1. FilesystemMiddleware 对超大的工具结果进行文件外置；
2. SummarizationMiddleware 在上下文接近窗口限制时生成摘要；
3. 摘要和外置文件进入原生 State/Backend；
4. 后续模型调用读取摘要和必要的文件引用。

项目不实现：

- 自定义 token 估算；
- `RECENT_K`、`SUMMARY` 或历史窗口字段；
- 手工截断消息；
- 自建 summary 表；
- 请求级 `history` 冷启动逻辑。

## 9. 删除语义

### 删除 Conversation

业务层删除 Message、Thought 和 Workspace/Artifacts。对应 Checkpoint 通过
稳定 thread ID 清理，Store 中的长期记忆不因 Conversation 删除而自动删除，
因为它属于 Account/App 或 EndUser 作用域。

### 删除 EndUser

业务层按 namespace 删除该 EndUser 的 Store 文件；同时清理 Conversation 和
Workspace。删除必须按服务端作用域执行，不能接受模型传入路径。

### 删除 App 或 Account

按 namespace 前缀清理 Store，删除对应 Checkpoint、Workspace、Skill 绑定和
业务消息。Store 清理可以是幂等后台任务，但不能改变运行时的租户隔离规则。

## 10. 代码定位

| 能力 | 代码 |
|---|---|
| 运行 Context 和资源 | `internal/core/agent_v2/runtime/models.py` |
| 资源装配 | `internal/core/agent_v2/runtime/assembly.py` |
| 图工厂 | `internal/core/agent_v2/runtime/factory.py` |
| Thread 配置 | `internal/core/agent_v2/runtime/thread_config.py` |
| Store namespace | `internal/core/agent_v2/runtime/memory.py` |
| Checkpoint | `internal/core/agent_v2/persistence/checkpoint.py` |
| Store 生命周期 | `internal/core/agent_v2/persistence/store.py` |
| Workspace 文件 | `internal/core/agent_v2/backends/workspace.py` |
| 事件翻译 | `internal/core/agent_v2/middleware/event_translation_middleware.py` |
