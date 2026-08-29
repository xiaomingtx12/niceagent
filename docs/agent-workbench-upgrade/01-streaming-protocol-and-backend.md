# 流式协议与后端实施计划

> 本文记录改造前的能力盘点和实施决策，文中的 `GraphExecutionDriver`、`QueueEvent` 生产链已经
> 完成迁移并删除。当前实现见 [旧流式执行链删除说明](./07-legacy-stream-removal.md)。

## 1. 当前能力与缺口

后端已经具备以下基础：

- `GraphExecutionDriver` 驱动 Deep Agents/LangGraph 运行；
- `QueueEvent.TODO_UPDATE`、`SUB_AGENT_DELEGATE`、`SUB_AGENT_END`；
- 旧 `AgentThought` 和 `MessageAgentThought` 曾承载 Todo、委派、层级和最终轨迹字段；
- 稳定 `thread_id`、LangGraph checkpointer、同步和异步子 Agent 配置；
- `QueueEvent` 到 SSE 的业务翻译边界。

目前的主要缺口不是“没有事件”，而是事件被压平且缺少运行协议：

| 能力 | 当前状态 | 工作台需要 |
| --- | --- | --- |
| 根 Agent 流式输出 | 已有 token/message SSE | 保留并归属到 root scope |
| Todo | 有更新事件 | 可重放的 Todo 快照/增量和版本顺序 |
| 子 Agent | 只有委派/结束摘要 | namespace、深度、父子关系、消息和工具事件 |
| 并行/嵌套执行 | UI 难以表达 | scoped event + 稳定 run/node identity |
| 断线恢复 | 无明确游标 | `event_id`/`sequence` + `since`/`Last-Event-ID` |
| 命令语义 | `/chat` 一次性请求 | thread/run/command/resume/cancel 语义 |
| 运行持久化 | 主要在结束后写入 | 运行状态权威记录 + 短期有序事件重放 |
| 浏览器事件安全 | 通用字段裁剪 | 固定安全投影和主体权限校验 |

`internal/core/agent_v2/execution/agent_executor.py` 直接消费
`CompiledStateGraph.stream_events(version="v3")` 的原始事件。协议层再根据实际事件中的
namespace、message 和 tool 元数据完成投影，不根据旧版 `messages/updates` 二元组猜测结构。

## 2. 原生能力决策表

| 需求 | 首选能力 | my-dify 责任 | 禁止事项 |
| --- | --- | --- | --- |
| Agent/Todo/SubAgent | Deep Agents 原生 Harness 和 Middleware | 装配、业务授权、展示投影 | 重写 Todo/SubAgent 状态机 |
| 线程状态 | LangGraph checkpointer | 配置 saver、绑定租户和 thread | 新建第二套对话状态存储 |
| 实时事件 | LangGraph `stream_events` / Agent Streaming Protocol | 协议接入和安全裁剪 | 扩展 QueueEvent 成平行标准 |
| 前端消费 | `@langchain/vue` + `HttpAgentServerAdapter` | Flask 兼容端点和 auth header | 直接让 `useStream` 消费旧 `/chat` SSE |
| 断线重放 | 协议 `since`/subscription 语义 | Redis Stream 短期缓存 | PostgreSQL 永久保存全部 token delta |
| 最终审计 | LangGraph state + 业务数据库 | `agent_run` 摘要、最终 trace 投影 | 用 Redis 替代权威业务数据 |
| HITL | LangGraph `interrupt()` / `Command(resume=...)` | 权限、审批记录、命令端点 | 自定义暂停/恢复状态机 |
| 文件 | Deep Agents Backend/FilesystemMiddleware | 文件授权、下载和发布策略 | 复制文件系统中间件 |

如果框架能力无法满足业务需求，实现前必须在本目录补充：缺失能力、评估过的原生
方案、失败原因和自动化验收用例。

## 3. 目标运行模型

核心标识职责如下：

- `thread_id`：长期会话和 LangGraph 检查点主键，同一会话多次运行共享。
- `run_id`：一次 command 执行实例，用于状态、取消、指标和审计。
- `event_id`：可供 SSE `Last-Event-ID` 使用的全局事件标识。
- `sequence`：同一 `run_id` 内严格递增的投影序号，用于去重和排序。
- `namespace`：LangGraph/子图作用域；前端依此构建父子执行树。
- `depth`：服务端按 namespace 推导或协议原生提供，只作查询和展示辅助。

典型流程：

```text
POST command
  -> 校验 app/thread/tenant/end-user 权限
  -> 创建 agent_run(status=queued)
  -> 启动 Deep Agent / LangGraph run
  -> stream_events(version=v3)
  -> 原生事件安全投影
  -> Redis Stream 追加有序事件
  -> SSE 实时发送
  -> 更新 agent_run 最终状态、Message 和快照
```

## 4. 协议端点

优先实现 `HttpAgentServerAdapter` 所需的 Agent Streaming Protocol 兼容端点。最终路径和
请求体以 Milestone 1 的 SDK 抓包/类型定义为准，建议业务路由如下：

```text
POST /web-apps/:token/threads/:threadId/commands
POST /web-apps/:token/threads/:threadId/stream

POST /apps/:appId/threads/:threadId/commands
POST /apps/:appId/threads/:threadId/stream
```

发布态通过 token + end-user 身份鉴权；创建者态通过登录账号、工作空间和 app 权限鉴权。
创建者和发布应用使用同一套固定事件投影，不接受客户端展示范围参数。

流订阅至少支持 SDK 要求的：

- channel；
- namespace；
- depth；
- since cursor；
- thread/run 关联；
- abort/cancel 与 resume command（在相应里程碑开放）。

平台自定义字段只能放在明确的扩展区域，例如 `metadata.my_dify`，不得修改官方字段含义。
候选扩展字段包括 `app_id`、公开 artifact 描述、业务展示名和脱敏状态，但不可包含数据库
实体对象或内部权限信息。

### 4.1 事件投影

服务端应从原生 v3 事件映射到协议 channel，而不是先压成 `QueueEvent` 再反向推断：

| 原生信息 | Agent Protocol 投影 |
| --- | --- |
| root messages/messages-tuple | `messages` 根对话增量 |
| Todo state/update | `values.todos` 计划快照或变更 |
| subgraph namespace start/end | 带 namespace 的 `lifecycle` 子 Agent 状态 |
| scoped message/tool event | 对应 namespace 的 `messages/tools` 事件 |
| file/backend artifact | `values.files` 工作区快照 |
| interrupt | `lifecycle` 中断状态 |
| run terminal state | `lifecycle` completed/failed/cancelled |

旧 QueueEvent 兼容层已经删除，客户端不得再根据 `SUB_AGENT_DELEGATE/END` 等旧事件拼装轨迹。

### 4.2 顺序、幂等和重连

- 每个投影事件写 Redis 成功后才向 SSE 客户端确认发送。
- 同一 run 的 `sequence` 严格单调递增；客户端用 `(run_id, sequence)` 去重。
- 重连优先读取 `Last-Event-ID`，也接受协议定义的 `since`。
- 游标仍在 Redis 保留窗口内时补发缺失事件，再切回 live stream。
- 游标过期时返回可识别的 reset/snapshot 结果，客户端重取当前 state，不静默丢事件。
- 多标签页订阅不能重复启动同一个 command。
- 慢消费者设置缓冲上限；超过上限主动断开并允许从游标恢复。

Redis Stream key 必须包含环境和租户隔离维度，TTL 与最大长度通过配置控制。不得把完整
提示词、密钥或未脱敏工具结果写入可被发布用户读取的 stream。

## 5. 数据所有权

| 存储 | 权威内容 | 生命周期 |
| --- | --- | --- |
| LangGraph checkpoint | thread state、interrupt、恢复位置 | 随线程策略 |
| PostgreSQL `agent_run` | run 状态、主体、时间、错误摘要、usage、最终游标 | 业务审计周期 |
| Redis Stream | 已裁剪的有序实时事件和断线重放 | 短期 TTL/长度限制 |

旧 `MessageAgentThought` 投影不属于 LangGraph 状态，也不属于 Agent Protocol。测试阶段不保留旧
历史兼容，因此模型与响应字段均已删除；运行详情通过协议事件、当前 state 和持久化 Run 事件读取。

建议新增 `agent_run` 最小字段：

```text
id, tenant_id, app_id, conversation_id, thread_id, run_id,
initiator_type, initiator_id, status,
started_at, completed_at, last_sequence, error_code,
usage_summary, trace_summary, created_at, updated_at
```

不在 PostgreSQL 逐 token 落库。最终内容仍进入消息体系；运行结束时存结构化摘要或外部
trace 引用。表结构必须沿用现有模型、迁移和软删除/审计约定。

## 6. 安全投影

投影发生在写入 Redis Stream 和数据库之前，而不是仅在 Vue 中隐藏。创建者和
发布应用固定输出 `messages/values/tools/lifecycle`；`values` 只保留消息、Todo 和文件。
系统消息不进入对话投影，工具消息保留给工作台执行详情使用；凭据、请求头、系统提示词和
内部异常栈递归脱敏。

还需落实：

- 所有查询同时校验 tenant、app、thread、run 和用户主体；
- 工具输入输出递归过滤敏感字段；
- 文件下载重新鉴权，不把 Backend 内部路径直接当公开 URL；
- 日志记录访问主体、run、订阅范围和裁剪策略版本；
- 错误对终端用户返回稳定错误码，内部栈只进入受控日志/trace。

## 7. 建议代码归属

当前实现归属如下：

| 责任 | 现有/建议位置 |
| --- | --- |
| 原生 graph 事件读取 | `internal/core/agent_v2/execution/agent_executor.py` |
| v3 事件到协议 channel 投影 | `internal/core/agent_v2/protocol/event_projector.py` |
| 固定安全投影 | `internal/core/agent_v2/protocol/event_sanitizer.py` |
| Redis 短期重放与通知 | `internal/repository/agent_workbench_redis_repository.py` |
| Run 业务编排 | `internal/service/agent_workbench_service.py` |
| Run 持久化 | `internal/service/agent_workbench_run_service.py` |
| 各来源 Run 构造 | `internal/service/agent_workbench_service.py` 内部方法 |
| 创建者/发布态/助手/OpenAPI HTTP 适配 | 对应拆分 handler 和 router 模块 |

`runtime/deep_agent_factory.py`、`runtime/subagent_spec_adapter.py` 和
`persistence/langgraph_checkpoint_provider.py` 应继续只负责
原生 runtime 装配，不承载 HTTP、Redis replay 或前端展示逻辑。

## 8. 后端测试范围

- v3 fixture：根 Agent、一个子 Agent、嵌套子 Agent、并行子 Agent、工具、Todo、文件、错误；
- 协议契约：command、stream、channel/namespace/depth/since；
- 顺序与幂等：重复订阅、断线、重连、过期 cursor、慢消费者；
- 权限：跨租户、跨 app、伪造 thread/run、文件越权；
- 持久化：queued/running/terminal 状态、异常恢复、服务重启；
- 清理：运行代码不得再引用旧 `/chat` SSE、QueueEvent 或 `task_id` 取消链；
- 负载：并发连接、事件速率、Redis 内存、首事件/首 token 延迟。

具体执行门禁见 [路线图与验收](./03-roadmap-and-acceptance.md)。
