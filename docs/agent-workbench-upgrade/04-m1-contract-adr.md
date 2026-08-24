# ADR M1：v3 事件与 Vue SDK 契约

## 状态

已验证，2026-08-23。

## 上下文

当前 Backend 依赖为 `deepagents==0.6.12`、`langgraph==1.2.9`、
`langchain_protocol==0.0.18`；Frontend 使用 Vue 3.4。工作台需要同时观察协调 Agent、
Todo、工具、同步子 Agent 和嵌套 namespace，旧 `QueueEvent` 平铺事件不足以支撑这些视图。

## 实测结果

### Python v3 raw protocol

`CompiledStateGraph.stream_events(..., version="v3")` 在当前版本可用，并返回可迭代的
`GraphRunStream`。每条 raw event 具备：

- `type="event"`；
- `method`：`values`、`messages`、`tools`、`lifecycle` 等 channel；
- `params.namespace`：root 为 `[]`，同步子 Agent 为形如 `tools:<run-id>` 的一层路径；
- `params.data`：channel 结构化数据；
- `seq`：同一 run 内从 1 开始递增的顺序号。

子 Agent lifecycle 数据实测包含 `graph_name`、`trigger_call_id` 和 `cause`；tool channel
包含 `tool_name`、`tool_call_id` 以及 started/finished 事件。消息 metadata 内的随机
checkpoint/run 标识不能作为业务主键。

### Python typed projection

`run.subagents` 能发现 Deep Agents `task` 委派，并提供：

- `name`：配置的 `subagent_type`；
- `path`：namespace path；
- `status`：started/completed/failed 等；
- `cause`：触发它的 tool call；
- `tool_calls`、`messages` 等 scoped projection。

同步消费时必须使用 `run.interleave("messages", "subagents")` 或按官方方式并发消费。若先
单独消费某一 projection，另一 projection 可能看不到已经被 pump 消费的事件。
`SyncTextProjection` 是可迭代文本投影，渲染前需要组合迭代内容，不能假定它是普通 string。

### Agent Protocol Python types

`langchain_protocol.EventStreamRequest` 是 TypedDict 工厂，实例结果通过下标读取而不是
属性读取。其字段覆盖工作台需要的 `channels`、`namespaces`、`depth`、`since`。

### Vue SDK

Frontend 锁定：

- `@langchain/vue@1.0.32`；
- `@langchain/core@1.2.9`；
- 间接依赖 `@langchain/langgraph-sdk@1.9.31`；
- `@langchain/protocol` 由 SDK 提供协议类型。

`HttpAgentServerAdapter` 支持 `apiUrl`、`threadId`、`defaultHeaders`、`fetch`、
`paths.commands/stream/state`。当前新增的 `createAgentWorkbenchAdapter` 使用：

```text
{apiPrefix}/apps/:appId/threads/:threadId/commands
{apiPrefix}/apps/:appId/threads/:threadId/stream
{apiPrefix}/apps/:appId/threads/:threadId/state

{apiPrefix}/web-apps/:token/threads/:threadId/commands
{apiPrefix}/web-apps/:token/threads/:threadId/stream
{apiPrefix}/web-apps/:token/threads/:threadId/state
```

Backend 已增加对应 Flask 端点，并完成 `agent_run/agent_run_event`、Redis Stream 短期重放、
PostgreSQL 降级和 cursor reset。当前仍保留单进程运行注册表作为本进程低延迟通道，跨进程
实时事件由 Redis Stream 承担；真实双进程集成与故障演练仍是 M3 门禁。

Redis 重放窗口默认配置为 600 秒（10 分钟），只用于断线恢复；任务状态、最终消息和
非 token 审计事件仍以 PostgreSQL 为准。SSE 等待下一轮任务时通过线程级 Redis Pub/Sub
通知唤醒，Redis 不可用时才退回本进程条件变量和 5 秒一次的低频数据库检查。

## 决策

1. 后端正式流读取使用 v3 raw protocol event 作为有序投影来源；typed projection 用于
   内部验证和可选的 scoped 业务逻辑，不从 `delegate/end` 文本推断层级。
2. 新前端统一使用 `HttpAgentServerAdapter` 和 `useStream`，保留现有 `ssePost` 作为旧链路。
3. Backend 的协议 adapter 必须透传 `seq`/`event_id`，并实现 `channels/namespaces/depth/since`。
4. 业务路由通过 adapter `paths` 注入；不修改官方协议字段语义。
5. namespace/run/checkpoint 中的随机标识只用于调试关联，不直接成为 `agent_run` 业务主键。
6. v3 当前仍标注 experimental，升级 `deepagents` 或 `langgraph` 前必须重跑 M1 fixture。

## 未决问题与 M2 前置门禁

- `thread_id` 与 app/end-user 的数据库关联模型（当前由现有 Conversation 权限校验承担）；
- Redis Stream client、TTL、消费组和多实例部署约束；
- `event_id` 是否由 Backend 生成，还是使用 `seq` 的可重建编码；
- state hydration 的权限裁剪和历史快照格式；
- `@langchain/vue` `useStream` 对 namespace selector 的正式类型映射。

这些问题解决前不接入正式页面、不删除旧 SSE；`agent_run` migration 留在 M2 下一切片。

## 自动化证据

Backend：

```powershell
Set-Location D:\spring_project\py\my-dify
.\.venv\Scripts\python.exe -m pytest -q test/internal/core/agent_v2/streaming/test_v3_contract.py
# 3 passed
```

Frontend：

```powershell
Set-Location D:\spring_project\py\my-dify-ui
npm run type-check
npm run test:unit -- --run src/features/agent-workbench/api/adapter.test.ts
# type-check passed; 2 tests passed
```
