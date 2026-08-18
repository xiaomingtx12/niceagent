# DeepAgents V2 实现进度与后续路线

> 更新时间：2026-08-17
> 本文只描述当前代码状态。异步 SubAgent 与 HITL 不属于 V1 交付范围。

## 1. 当前结论

DeepAgents V2 已完成底座切换、Workspace/Skills/Memory 统一运行时、同步多 Agent、Todo/委派轨迹闭环、官方 Sandbox 装配和 P3 代码内安全边界。当前尚未达到生产就绪，剩余重点是外部指标/告警适配，以及真实 PostgreSQL、模型和 Sandbox provider 部署环境验证。

- **已并入分支基线**：DeepAgents 原生主图、统一 runtime、fail-closed Backend、内置只读 Skills、Workspace、结构化 Memory、原生摘要/checkpoint、同步 SubAgent 和资源管理 UI。
- **当前工作区开发完成待总体验证**：统一 `agent_v2` 目录、轨迹/Todo 全链路、模块职责拆分、Workspace 配额/并发保护、私有审计命名空间和统一结构化 telemetry。
- **当前静态状态**：本轮只完成源码、文档、迁移和导入边界调整，按要求未执行测试。
- **主要剩余项**：接入外部指标/告警和审计留存；真实 PostgreSQL 升降级、checkpoint 连续对话、真实模型同步委派、部署沙箱与全入口冒烟。

## 2. 分阶段进度

| 阶段 | 状态 | 已落地 | 剩余 |
|---|---|---|---|
| R0 · DeepAgents 底座 | 功能完成 | 原生 `create_deep_agent`、Memory/Summarization、Backend、checkpointer、稳定 thread、统一历史和事件边界 | 真实 PostgreSQL saver 连续会话与摘要溢出 E2E |
| R1 · Workspace 与文件权限 | 功能完成 | 版本化 Workspace、会话隔离、配额、PG advisory lock、冲突重试、私有保留目录、StateBackend default、显式路由和原生 permissions | 真实 PG 并发压测、审计留存和跨租户扩展测试 |
| R2 · Skills 与文件 Memory | 功能完成 | 公共只读 Skill 包、App/角色绑定、`/memories/AGENTS.md`、Store namespace 隔离和原生 `memory=` 投影 | 真实 Store 删除任务和部署环境运行验证 |
| R3 · 同步多 Agent | 功能完成待 E2E | 同步零历史委派、工具/Skill/文件权限、独立模型、稳定 delegation trace、Todo 事件持久化和 UI | 真实模型、多入口和故障注入演示 |
| R4 · 安全、监控与收尾 | 进行中 | fail-closed Runtime、由 DeepAgents 原生 FilesystemMiddleware 按 Backend 能力隐藏/开放 `execute`、Skill/Memory 上传、Workspace 同步、结构化 telemetry | 真实 provider 网络/资源策略 E2E、外部指标告警、完整审计、迁移回滚和部署验证 |

## 3. 当前代码边界

后续开发必须复用这些单一入口，不新增平行实现：

1. `internal/core/agent_v2/runtime/factory.py`：DeepAgents 图装配。
2. `internal/service/agent_runtime_service.py`：Workspace、Skills、Memory、Context、Store、permissions 与 saver 的业务装配。
3. `internal/core/agent_v2/middleware/event_translation_middleware.py`：薄 Hook 适配。
4. `internal/core/agent_v2/events/`：会话关联、纯事件翻译和公共序列化。
5. `internal/core/agent_v2/backends/`：DeepAgents Backend 协议适配。
6. `internal/schema/agent_thought_schema.py`：历史轨迹 API 的统一序列化。
7. `internal/core/agent_v2/backends/sandbox.py`：官方 Sandbox provider、文件上传、Workspace 同步和生命周期。

Backend 内部继续按职责复用：`workspace_path.py` 负责路径 ACL，
`persistence/workspace.py` 负责版本存储，`backends/read_only.py` 负责只读目录协议。
Skill 和 Memory 的 Backend 适配器归入 `backends/`；命令执行由 DeepAgents 官方
Sandbox Backend 提供，项目不注册自定义脚本工具。

## 4. 后续更新顺序

### P0/P1/P2 · 已完成的功能边界

- 已移除 pgvector、自定义 context window、TokenBufferMemory 和数据库 Skill 正文链路。
- App/Debug、Assistant、WebApp、OpenAPI、WeChat 统一使用 `AgentRuntimeService`；WorkflowTool 作为工具进入这些 Agent 入口，当前没有独立的 Workflow Agent 运行入口。
- 原生 `memory=`、`skills=`、`subagents=`、`backend=`、`permissions=`、`store=` 和 `checkpointer=` 已接入。
- `message_agent_thought` 通过 `d_v2_008` 增加 Agent/委派字段和 Todo JSON 快照；同步阶段数据库 `task_id` 保持为空。
- `TODO_UPDATE` 只从成功的原生 `write_todos` Command 状态产生，并和委派事件一起持久化、SSE 推送和前端展示。

### P3 · 模块内聚与生产安全

1. [已完成] `workspace.py` 已拆为路径/ACL、版本仓储和 DeepAgents Backend 适配。
2. [已完成] 自定义脚本执行链已删除；官方 Sandbox provider 作为 `CompositeBackend.default`，原生 FilesystemMiddleware 按 Backend 能力过滤 `execute`。
3. [已完成] `BaseAgent` 的输入装配、结果聚合、线程驱动和 Backend 生命周期已拆成独立协作者。
4. [部分完成] Workspace 已增加配额、私有审计目录、并发版本锁/重试和隔离测试；真实 PostgreSQL 并发压测与完整文件审计留待 P4。
5. [部分完成] 事件、Workspace 和 Sandbox 生命周期已统一到 vendor-neutral telemetry Sink；外部指标 exporter、告警和摘要/Memory 专项指标留待部署集成。

**退出条件**：大文件职责边界清晰，新增公共方法有简短方法级注释，安全与指标测试通过。

### P4 · 生产验证与文档收口

1. 在真实 PostgreSQL 执行完整 Alembic upgrade/downgrade 和索引核验。
2. 验证 checkpoint 两轮连续对话、空线程 bootstrap、无重复消息和摘要 offload/reload。
3. 使用真实模型验证同步委派、Todo 更新、工具/Skill 最小权限和失败语义。
4. 对 Debug、Assistant、WebApp、OpenAPI、WeChat，以及携带 WorkflowTool 的 Agent 场景做入口冒烟。
5. 运行全量后端回归、前端构建、部署沙箱和回滚演练。

**退出条件**：安全、迁移、可观测性和全入口证据均可重复执行后，完整方案才标记生产就绪。

## 5. 当前验证边界

本轮按用户要求不执行测试、迁移升级、真实模型调用、数据库连接或 Sandbox
运行。后续如需要验证，再单独执行项目既有回归和部署检查，不在重构阶段
自行修改测试文件。

## 6. 2026-08-17 静态收口记录

- `compileall` 已通过，覆盖 Agent V2、主要入口服务和新迁移。
- 关键运行时模块导入扫描已通过。
- Agent V2、入口服务和业务模型中已无 `history`、`iteration_count`、
  `AgentRuntimePlan`、`MemoryFact` 或 `conversation_history` 运行引用。
- Alembic：当前最新迁移为 `d_v2_009_remove_memory_facts`，删除旧业务事实表，
  长期记忆改由 LangGraph Store 承载。
- 真实模型连续对话/委派、真实 Sandbox provider、全 HTTP 入口冒烟仍需在目标环境执行；当前状态不得标记为生产就绪。
## 2026-08-17 Refactor Progress

- Runtime context is wired through `context_schema` and `context=`.
- Store memory access resolves the namespace from `AgentRunContext`.
- Sandbox scope is stable per thread, anonymized, reusable, and TTL-managed.
- Request shutdown no longer deletes a reusable remote Sandbox.
- Official model retry, tool retry, tool error, and tool call limit middleware
  are configured in the single DeepAgents factory.
- No tests were run in this refactor pass by request.
- The obsolete `memory_facts` table is removed by migration
  `d_v2_009_remove_memory_facts`; long-term memory uses `/memories/` with a
  tenant-scoped LangGraph Store namespace.
