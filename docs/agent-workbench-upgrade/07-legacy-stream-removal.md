# 旧流式链路删除与 Agent Protocol 统一说明

## 1. 改造结果

调试工作台、发布 WebApp、Creator 辅助 Agent、OpenAPI 和微信后台调用现在共享同一套运行模型：

```text
HTTP / SDK command
  -> AgentWorkbenchService 创建 AgentRun
  -> AgentAssemblyService 装配 Deep Agent
  -> AgentExecutor.stream_events(version="v3")
  -> AgentProtocolEventProjector
  -> messages / values / tools / lifecycle
  -> Redis Stream 短期重放 + PostgreSQL 持久状态
  -> Agent Protocol SSE
```

不存在“工作台走 Agent Protocol、普通聊天再投影为旧 SSE”的第二条链路。旧 `/chat`、`task_id` 停止
接口和 `agent_message / agent_action / agent_end` 响应均已移除。

## 2. 删除的旧机制

以下组件属于旧的自定义事件总线或兼容层，已经删除：

- 进程内 `Queue + Thread + EventSession` 执行链；
- `EventTranslationMiddleware` 和旧事件翻译器；
- `AgentThought / AgentEvent / AgentResult` 运行时兼容模型；
- `AgentInvocationEventProjector` 和 `AgentInvocationEventResp`；
- `AgentResultAccumulator`；
- `AgentInvocationCancellationRepository`；
- 调试、WebApp、辅助 Agent、OpenAPI 的旧聊天与停止 handler；
- 前端旧 SSE reducer、旧聊天 hooks 和旧预览组件。

旧 `MessageAgentThought` 模型、`message_agent_thought` 表映射和消息响应中的 `agent_thoughts` 字段已删除。
测试阶段不保留历史数据兼容；运行详情由 Agent Protocol 事件、当前 state 和 `AgentRunEvent` 恢复数据提供。

## 3. 为什么删除独立取消 Repository

`agent_invocation_cancellation_repository.py` 只服务旧协议：它把临时 `task_id` 与调用者写入 Redis，再写一个
短期停止标记。迁移后每次执行在 command 阶段就已经创建并返回持久化 `AgentRun.id`，因此再维护一个
`task_id` 身份体系会产生两套运行状态。

当前取消链路为：

```text
POST /{scope}/threads/{thread_id}/runs/{run_id}/cancel
  -> 按 command 返回的 run_id 查询 AgentRun
  -> 校验 Run 对应应用、线程和调用来源
  -> AgentRun.status = cancelling
  -> Redis 写 run_id 取消信号
  -> 执行 worker 在事件边界观察信号
  -> 关闭 v3 生成器和 Backend
  -> 发布 lifecycle(cancelled)
  -> AgentRun.status = cancelled
```

PostgreSQL 中的 `AgentRun` 是取消状态权威；Redis 只是跨 worker 的低延迟通知。Redis 不可用时，执行 worker
仍会低频读取数据库状态完成取消。

## 4. Redis 在新链路中的职责

Redis 不执行 Agent，也不保存最终业务事实，只承担三项短期协作职责：

1. `Redis Stream` 保存最近一段协议事件，支持 SSE 断线后按 `seq` 重放；
2. `Pub/Sub` 通知其他 worker 某个 thread 出现新 Run，减少数据库轮询；
3. 按 `run_id` 发送取消信号，让执行 worker 尽快停止。

最终消息、Run 状态、最新 state 快照和需要审计的事件仍由 PostgreSQL 保存。进程内有界事件缓冲只优化
同 worker 订阅，不承担跨进程一致性。

## 5. 各入口的协议路由

| 入口 | Agent Protocol 与 Run 控制前缀 |
| --- | --- |
| 调试 | `/apps/{app_id}/threads/{thread_id}` |
| WebApp | `/web-apps/{token}/threads/{thread_id}` |
| 辅助 Agent | `/assistant-agent/threads/{thread_id}` |
| OpenAPI | `/openapi/apps/{app_id}/threads/{thread_id}` |

所有入口使用同一作用域下的 `/threads/{thread_id}/runs/{run_id}/cancel` 取消指定 Run。`thread_id` 由
Agent Protocol 客户端生成；服务端在首次 command 时校验作用域并按需创建业务会话。command 返回
`run_id`，官方 SDK 的 `stream.stop()` 会直接使用该标识发送取消请求，SSE 事件使用稳定的
`event_id / seq / namespace / method / params`。

## 6. 分层职责

| 模块 | 职责 |
| --- | --- |
| `schema/agent_runtime_schema.py` | Service 到 Agent 装配层的已校验调用参数 |
| `schema/agent_workbench_schema.py` | command、subscription、state、分页和取消 DTO |
| `service/agent_assembly_service.py` | Agent、模型、工具、SubAgent 和 Backend 装配 |
| `service/agent_workbench_service.py` | Run 排队、v3 消费、SSE、终态和取消编排 |
| `service/agent_workbench_service.py` | 各调用来源的会话、消息和 Run 构造 |
| `repository/agent_workbench_redis_repository.py` | Redis Stream、Pub/Sub 和 run_id 取消信号 |

Schema 不再存放内部执行实体，Repository 不再维护另一种 Run 身份，Handler 只做 HTTP 适配。

## 7. 迁移与兼容结论

这是公开传输协议的替换，不向旧 `/chat` SSE 保持兼容。调用方需要按顺序迁移为：

1. 生成并持久化 `thread_id`；
2. 调用 `commands` 提交 `run.start`；
3. 订阅 `stream`，按 `seq` 维护游标；
4. 首次加载或重连时读取 `state`，滚动历史读取 `messages`；
5. 调用 SDK `stream.stop()`，按 command 返回的 `run_id` 停止对应 `AgentRun`。

本次没有创建、修改或执行数据库迁移文件。

由于 ORM 已删除 `message_agent_thought` 表定义，后续由开发者自行执行 `flask db migrate` 生成删表迁移，
检查生成结果后再决定何时执行 `flask db upgrade`。
