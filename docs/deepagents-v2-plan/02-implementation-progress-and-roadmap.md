# DeepAgents V2 实现进度与后续路线

> 更新时间：2026-07-28
> 本文只描述当前代码状态。功能只有在代码、迁移、权限、测试和必要的端到端验证同时闭环后，才标记为“已完成”。仅存在于未提交工作区的实现统一标记为“开发中”。

## 1. 当前结论

DeepAgents V2 已完成可运行的底座切换、同步多 Agent 垂直切片和一轮运行时加固，但完整方案仍处于开发中，暂不能按生产就绪验收。

- **已并入当前分支基线**：DeepAgents 主图工厂、原生 backend 路由、稳定 thread 配置、事件翻译、同步 sub-agent 配置与 Creator 团队编辑器。后端基线提交为 `b509941`、`16e8db7`，前端基线提交为 `2a2d62a`、`008506f`。
- **当前工作区正在开发**：Skills、Workspace、Memory、统一运行时装配、原生摘要历史文件、同步 SubAgent 安全收口，以及配套 Creator / WebApp 资源界面。这些代码尚未形成完整提交闭环。
- **当前同步多 Agent 结论**：只开放 DeepAgents 原生同步 `task` 委派；默认 `general-purpose` 已通过 Harness Profile 关闭；父历史复制、`SUMMARY / RECENT_K`、自建线程超时与未闭环异步配置已经移除。完整边界见 `05-synchronous-multi-agent-implementation.md`。
- **当前验证结果**：同步 SubAgent、Harness Profile、factory、统一 runtime 和配置聚焦测试共 31 项通过；仍需执行完整 Agent V2 回归、前端生产构建和真实模型端到端委派。当前拍板方案将移除 pgvector，而不是把它作为交付依赖。
- **主要未闭环项**：撤回 pgvector、自定义 context-window 和数据库 Skill 正文链路；迁移链验证；内置 Skill Web 目录与主/子 Agent 挂载；DeepAgents 原生 Memory/Compression 集成；全入口端到端回归；同步委派轨迹字段、`TODO_UPDATE`、安全隔离验收和监控告警。异步 SubAgent 与 HITL 已移出当前交付范围。

## 2. 分阶段进度

| 阶段 | 状态 | 已落地 | 未闭环 |
|---|---|---|---|
| R0 · DeepAgents 底座 | 基本完成 | `create_deep_agent` 主图、业务中间件、原生 backend、可选 checkpointer、稳定 thread、统一消息历史、SSE 事件翻译；旧 FunctionCall/ReACT 实现已移除，TokenBufferMemory 和自定义摘要包装已移除 | 真实 PostgreSQL saver 连续两轮对话、所有调用入口和摘要溢出恢复仍需端到端验证；独立 `app_context_loader.py` 未按原规划保留，当前职责由统一运行时和上下文中间件承担 |
| R1 · Workspace 与文件权限 | 开发中 | `workspace_files` 模型/迁移/版本服务、DB WorkspaceBackend、Creator 与 WebApp 文件 API、会话隔离、原生摘要历史文件、Linux Bubblewrap 基础 | 当前默认 `MyDifySandboxBackend` 会暴露通用 `execute`，且未传 `permissions=`；必须改成拒绝型 default + 显式命名空间路由，补齐 Backend ACL、配额、审计、并发版本和跨租户测试；Windows 生产执行必须关闭 |
| R2 · Skills 与结构化 Memory | 开发中 | 当前已有数据库 Skill CRUD 和前端资源面板原型；`memory_facts` 的 end-user 归属、CRUD、自动提取任务和 Creator 只读接口 | 删除数据库 Skill 正文链路，建立公共只读 `BuiltinSkillCatalog`；Web 可查看并挂载到主 Agent，SubAgent 选择其子集；脚本改走受控 `run_builtin_skill_script`；pgvector 改为 PostgreSQL 权威事实 + DeepAgents `memory=`/Backend 投影 |
| R3 · 同步多 Agent | 开发中 | 同步 sub-agent 持久化、显式工具白名单、角色级 Skill 投影、可选独立模型、只读文件权限、Creator 同步团队编辑器、委派/结束事件；默认 `general-purpose` 已关闭；上下文固定为原生零历史继承 | `message_agent_thought` 尚无 `agent_id / agent_type / parent_agent_id / delegation_id` 字段；没有 `TODO_UPDATE`；仍需真实模型委派、跨入口和主 Backend 权限 E2E |
| R4 · 安全、监控与收尾 | 未开始 | 同步 schema 和 UI 已拒绝异步字段；当前不提供 HITL | 完成生产 Backend、安全沙箱、指标告警、迁移回滚和全入口验证；异步 SubAgent 与 HITL 以后单独立项 |

## 3. 已经可以继续复用的基础

后续开发应在现有实现上收口，不重新实现 DeepAgents 已提供的能力。

1. `internal/core/agent_v2/factory.py` 是 DeepAgents 图构建入口，继续使用原生 `backend=`、`skills=`、`subagents=` 和 `checkpointer=`。
2. `internal/service/agent_runtime_service.py` 是 Workspace、Skills、Memory、上下文和 saver 的统一装配入口，所有聊天入口必须复用它。
3. `EventTranslationMiddleware` 是 LangGraph/DeepAgents 到现有 QueueEvent/SSE 的唯一翻译边界。
4. `MyDifyWorkspaceBackend` 继续作为持久化适配基础；线上默认 Backend 不实现 `SandboxBackendProtocol`，脚本只通过受控 `run_builtin_skill_script` 复用 Linux 隔离能力，完整边界见 `04-skill-filesystem-backend-permission-plan.md`。
5. `AppConfigVersion.config.subagents` 继续作为单 Agent、多 Agent和 Plan 模式的统一配置，不新增 app 类型、成员表或 plans 表。

## 4. 后续更新顺序

### P0 · 先恢复可验证状态

1. 删除 pgvector 依赖、向量字段/迁移和自定义 `context_window_calculator.py`，统一使用 DeepAgents 原生 Memory、Summarization 和模型 profile。
2. 把 `memory_facts` 收口为 PostgreSQL 权威业务事实，并通过 Backend 投影只读 `/memories/profile.md`；暂不新增向量索引。
3. 跑通 Agent V2、Memory、Skills、Workspace、Handler 和 Service 聚焦测试，先修复收集错误和真实失败。
4. 在 PostgreSQL 上验证 `d_v2_001` 至 `d_v2_007` 升降级链、关系索引和存量数据兼容。
5. 复跑前端类型检查/生产构建，并做后端 API 与前端字段契约核对。

**退出条件**：聚焦后端测试、迁移测试、前端构建和 `git diff --check` 全部通过，当前开发中代码形成边界清晰的提交。

### P1 · 闭环持久化与统一运行时

1. 验证 App、Assistant、WebApp、OpenAPI、WeChat 和 Workflow Agent 六类入口都走统一 runtime builder。
2. 验证稳定 thread 下的 checkpoint 两轮连续性、空线程 bootstrap 和无重复消息。
3. 验证 Workspace 版本、会话隔离、历史 offload/reload；验证 Skills 只加载启用版本。
4. 验证 Memory 的 end-user 隔离、Profile/importance/关键词召回、自动提取、投影失效和软删除；Weaviate 不可用时必须正常回退 PostgreSQL。
5. 将内置 Skill 收口到公共只读目录，并验证每个 App/Agent 只看到绑定子集；数据库不再保存同一 Skill 的第二份权威正文。

**退出条件**：Acceptance Matrix 中 Skills、Workspace、Context、Memory、Compression、Checkpoint 和 Invocation coverage 各有自动化测试及至少一条端到端证据。

### P2 · 完成 R3 多 Agent 产品能力

1. 给 `message_agent_thought` 补齐 `agent_id / agent_type / parent_agent_id / task_id`，同步模型、迁移、序列化与前端轨迹分组。
2. 从 TodoList state 产生 `TODO_UPDATE`，确保事件持久化、SSE 推送和前端步骤展示一致。
3. 对同步 sub-agent 的工具白名单、角色级 Skills、独立模型、零历史继承和只读文件权限做真实模型委派测试。

**退出条件**：同步多 Agent 从配置、发布、运行到轨迹完整可演示，且单 Agent 行为无回归。当前验收不包含 HITL。

### 延后项 · 异步 SubAgent 与 HITL

这两项不属于当前 TODO 和验收矩阵。未来重新立项时：

1. 异步 SubAgent 先核对届时 DeepAgents/LangGraph 原生能力，再设计任务注册、查询、取消和恢复。
2. HITL 优先复用 LangGraph Interrupt/Resume，再补平台审批、事件和 UI 适配。
3. 不得在当前同步配置、API 或 UI 中保留不可用占位开关。

### P3 · 安全与生产化收尾

1. 线上默认 Backend 切为不可执行 Backend，`CompositeBackend.default` 拒绝未路由路径，`create_deep_agent` 显式传入带兜底拒绝的 `permissions=`。
2. 在部署镜像中验证公共 Skill 目录只读、Bubblewrap/容器、精确输入输出挂载、资源限制、禁网和进程树清理；Windows 生产脚本执行关闭。
3. 补齐路径穿越、软/硬链接、Unicode、跨 app/account/end-user、未绑定 Skill、命令注入、恶意脚本和超时资源耗尽测试。
4. 增加 token、工具调用、sub-agent 延迟/失败率、摘要触发、workspace 容量、脚本沙箱和 memory 召回指标及告警。
5. 完成全量后端回归、前端构建、真实凭据冒烟、部署迁移和回滚演练。

**退出条件**：安全门禁、可观测性、迁移和回滚均可重复执行，完整方案才标记为生产就绪。

## 5. 当前验证命令

```powershell
# 同步依赖后再执行后端聚焦回归
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m pytest `
  test\internal\core\agent_v2 `
  test\internal\core\memory `
  test\internal\handler\test_memory_fact_handler.py `
  test\internal\handler\test_skill_handler.py `
  test\internal\handler\test_workspace_file_handler.py `
  test\internal\service\test_agent_runtime_service.py `
  test\internal\service\test_memory_fact_service.py `
  test\internal\service\test_skill_service.py `
  test\internal\service\test_workspace_file_service.py `
  test\internal\service\test_subagent_config.py -q

# 前端
npm --prefix ..\my-dify-ui run build
```

当前一次同步多 Agent 聚焦验证结果：31 项测试通过。完整 Agent V2、Memory、Skills、Workspace 回归和前端构建仍需在本轮收尾时执行。由于 pgvector 方案已经被项目硬约束否决，最终验证必须在移除该依赖和自定义压缩代码后重新执行。
