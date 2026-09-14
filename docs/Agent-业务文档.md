# Agent 业务文档

> 本文档依据当前代码实现整理（`internal/core/agent_v2`、`internal/service/agent*`、`internal/handler/agent*`、`internal/router`、`internal/model`、`config`），不引用任何历史设计文档或仓库内 Markdown。
> 标注 `【问题】` 的条目是阅读代码时发现的实现缺陷，仅作提醒，未做修改。

---

## 1. 定位

平台上的「Agent 应用」是一个由 DeepAgents（`deepagents 0.6.12` + `langgraph 1.2.9` + `langchain 1.3.14`）驱动的自主任务执行体：

- 一个 App 持有一份 **Agent 定义**（模型 / 预设提示词 / 工具 / 知识库 / 工作流 / Skills / SubAgents / 审核 / 长期记忆 / 用户提问开关）。
- 用户在某个 **Conversation（会话）** 中提交一次任务，产生一条业务 `Message` 和一个 **Run**。
- Run 在后台线程里装配出一张一次性的 LangGraph 图并执行，期间以 **事件流（SSE）** 向前端推送过程。
- 执行过程可被**取消**；Agent 缺信息时可**中断并向前端提问**，用户填表后续跑同一个 Run。

代码分层：

| 层 | 目录 | 职责 |
| --- | --- | --- |
| 协议适配 | `internal/handler/agent*`、`internal/service/agent_protocol` | HTTP/SSE 边界，命令分派，事件与状态投影 |
| 业务编排 | `internal/service/agent` | Run 生命周期、模型与能力授权、附件、装配入口 |
| Agent 内核 | `internal/core/agent_v2` | 与平台无关的图装配、执行、事件规范化 |
| 持久化 | `internal/model`、`internal/core/agent_v2/persistence` | Run/事件表、LangGraph checkpoint 与 Store |

依赖方向是单向的：**平台 → Core**，Core 不引用 `internal/service`（`internal/service/agent_protocol/__init__.py` 的模块说明即为此约束）。

---

## 2. 核心概念

### 2.1 InvokeFrom（调用入口）

`internal/entity/conversation_entity.py:84-91`

| 值 | 含义 | 使用的 HTTP 入口 |
| --- | --- | --- |
| `debugger` | 应用调试页 | `/apps/<app_id>/conversations/...` |
| `web_app` | 发布态 WebApp | `/web-apps/<token>/conversations/...` |
| `assistant_agent` | 内置「应用构建助手」 | `/assistant-agent/conversations/...` |
| `creator_task` | Creator 任务中心 / 内部 SDK | `/tasks/apps/<app_id>/conversations/...`、`/agent-protocol/...` |
| `service_api` | 开放 API（服务方调用） | `/openapi/apps/<app_id>/conversations/...` |

`invoke_from` 会写入 `agent_run.invoke_from`，并作为**运行查询的隔离键**贯穿几乎全部 Run 查询（最新 Run、事件续传、模型覆盖、状态订阅、SSE 通知信号 key）。同一个会话在不同入口下看到的是各自独立的 Run 序列。

### 2.2 关键实体

| 实体 | 位置 | 说明 |
| --- | --- | --- |
| `AgentConfig` | `core/agent_v2/entity/agent_config.py:48` | 已解析的 Agent 定义（模型/工具/Skills/SubAgents），由业务层完成授权 |
| `AgentRunContext` | `core/agent_v2/entity/run_context.py:42` | 单次运行的信任上下文：身份、记忆开关、规划开关、提问开关、已确认答案 |
| `AgentInvocation` | `entity/agent_invocation.py:13` | 创建 Run 时冻结的完整执行输入（含指纹），后台装配不依赖 HTTP 请求 |
| `AgentRunExecution` | `entity/agent_run_entity.py:67` | 当前进程持有的执行片段（`run_id` 稳定，每次恢复分配新 `executor_id`） |
| `AgentExecutionResult` | `core/agent_v2/entity/execution_result.py:8` | 内核交付结果：status / interrupts / answer / state / usage / error |
| `AgentEvent` | `core/agent_v2/entity/agent_event.py:7` | 内核规范化事件（channel + data + sequence + namespace） |
| `WorkbenchEvent` | `schema/agent/agent_workbench_schema.py:294` | 对外协议事件（`type/event_id/seq/method/params`） |

### 2.3 业务模型（应用配置）

Agent 相关配置存在 `app_config` / `app_config_version` 两张表的 JSONB 列中（`internal/model/app.py:153-191`）：

| 列 | 默认值 | 含义 |
| --- | --- | --- |
| `model_config` | `{}` | 模型引用（provider / model / parameters / provider_connection_id） |
| `preset_prompt` | `''` | 业务指令 |
| `skills` | `[]` | 平台内置 Skill 绑定列表 `{name, version, digest, enabled}` |
| `subagents` | `[]` | SubAgent 编排配置 |
| `tools` | `[]` | 插件工具绑定（≤5） |
| `workflows` | `[]` | 工作流绑定（≤5） |
| `datasets` / `retrieval_config` | `[]` / `{semantic,10,0.5}` | 知识库与检索参数（≤5） |
| `long_term_memory` | `{enable: false}` | 长期记忆开关 |
| `user_input` | `{enable: true}` | 主 Agent 是否可向用户提问 |
| `review_config` | 见下 | 关键词审核配置 |
| `opening_statement` / `opening_questions` / `suggested_after_answer` | | 会话展示类配置 |

`review_config` 默认结构（`internal/entity/app_entity.py:57-67`）：

```json
{
  "enable": false,
  "keywords": [],
  "inputs_config": {"enable": false, "preset_response": ""},
  "outputs_config": {"enable": false}
}
```

### 2.4 应用状态与配置版本

- `AppStatus`：`draft` / `published`（`entity/app_entity.py:14`）。
- 草稿配置在 `app_config_version`（`config_type='draft'`），发布配置在 `app_config`。
- 入口与版本的对应关系（`service/agent/agent_definition_service.py:44-69`）：
  - `DEBUGGER` → 草稿运行配置；
  - 其余入口 → 已发布运行配置；
  - `ASSISTANT_AGENT` → **不访问 App 表**，直接返回代码内置定义（名称「应用构建助手」）。

---

## 3. 调用入口与鉴权

所有入口共用同一套工作台能力（`AgentRunService` + `AgentRunQuery`），只在**身份解析**和**固定 invoke_from** 上不同。

| 入口 | 身份来源 | 会话归属校验 |
| --- | --- | --- |
| 调试页 | `current_user` | `AppService.get_debugger_conversation`（App 所有权 + `invoke_from=debugger`） |
| WebApp | `current_user` + `EndUser` | `WebAppService.get_owned_web_conversation`（token + 会话） |
| 辅助 Agent | `current_user` | `AssistantAgentService.get_owned_conversation`（`app_id = ASSISTANT_AGENT_ID`） |
| Creator 任务 | `current_user` | `CreatorTaskService.get_owned_conversation` |
| 开放 API | `current_user`（来自 API Key） | `ConversationService.get_service_api_conversation` |
| 内部 SDK | `X-MyDify-Internal-Token` | `InternalAgentProtocolService`（签名声明 + 账号/父应用/目标应用复核） |

**权限边界原则**：客户端命令体不能覆盖账号或来源——Handler 从命令体中只取业务参数，身份一律由登录态/凭证提供，`invoke_from` 由路由硬编码传入。

鉴权由 blueprint 决定（`internal/middleware/middleware.py:24-60`，注册于 `internal/server/http.py:85`）：

- `my_dify` blueprint → `Authorization: Bearer <access_token>`，用 `JwtService.parse_token` 解析为 `Account`；
- `openapi` blueprint → `Authorization: Bearer <api_key>`，用 `ApiKeyService.get_api_by_by_credential` 解析并校验 `is_active`；
- 其余 blueprint → 不认证。

**例外**：`/agent-protocol/*` 路由注册在 `my_dify` blueprint 上但**没有 `@login_required`**，因此 flask_login 不会参与，唯一凭证是内部 HMAC Token。

### 3.1 内部 Agent Protocol 凭证

`service/internal_agent_protocol/auth.py`

- 格式：`<base64url(JSON payload)>.<base64url(HMAC-SHA256)>`（两段，均无 padding），payload `{v:1, account_id, parent_app_id, target_app_id, exp}`。
- 校验：`hmac.compare_digest` 签名比对 + `v == 1` + `exp` 为非布尔整数且未过期；密钥取 `AGENT_INTERNAL_PROTOCOL_SECRET`（空值回退 `JWT_SECRET_KEY`），TTL 取 `AGENT_INTERNAL_PROTOCOL_TOKEN_TTL_SECONDS`（下限 60 秒）。
- 签发后由 `build_internal_agent_transport` 强制写入 `X-MyDify-Internal-Token`，并**剔除配置里任何大小写变体的同名 header**，防止持久化配置伪造内部凭证。
- 每次调用还会复核：账号存在、父应用属于该账号、目标应用属于该账号且 `status = published`。
- 运行载荷还要求 `assistant_id == "mydify:published:<target_app_id>"`。

### 3.2 入口鉴权的差异

- Creator 任务中心、辅助 Agent、开放 API 的 `commands/stream/state/model-settings` 均要求登录态（`@login_required`）。
- `assistant-agent/conversations`（创建会话）限流 `rate_limit(config_key="user")`，且装饰器在 `@login_required` **外层**（限流先于认证执行）。
- 测评提交限流 `rate_limit(config_key="chat")`，在 `@login_required` 内层。
- 限流器在 Redis 不可用时**放行**并记录 `限流器不可用，跳过限流检查`。

---

## 4. Run 生命周期

### 4.1 状态机

`AgentRunStatus`（`entity/agent_run_entity.py:16-39`）：

```
                 start_run
      (无) ─────────────────► running ──────────────► completed
                               │  ▲                    failed
              respond_input    │  │  queue_resumed     cancelled
              (提交表单)        ▼  │
                          waiting_input
                               │
              cancel_run       │
              (待填任务直接取消) ▼
                           cancelled ──reopen_input──► waiting_input
```

- `AGENT_RUN_ACTIVE_STATUSES = (running,)` —— 唯一持有执行权（`executor_id`）的状态。
- `AGENT_RUN_OCCUPYING_STATUSES = (running, waiting_input)` —— 仍占用会话、不允许同会话启动新任务。
- `AGENT_RUN_TERMINAL_STATUSES = {completed, failed, cancelled}`。
- **取消不是一个状态位**，而是 `agent_run.cancel_requested_at` 时间戳；取消请求发出后 Run 仍显示 `running`，直到执行线程收敛。

数据库 CHECK 约束与状态机一致（`internal/model/agent_run.py:35-63`）：
- `status` 枚举；
- `ck_agent_run_executor`：`running` 必须有 `executor_id`，其余四态必须为 `NULL`；
- `ck_agent_run_finished_at`：终态必须有 `finished_at`，`running` / `waiting_input` 必须为 `NULL`；
- `last_event_sequence >= 0`；三个 JSONB 列必须是 object。

### 4.2 一个 Run 的完整生命周期

1. **创建（`start_run`）**（`agent_run_service.py:95-134`）
   - 先 `_settle_orphan_runs(conversation_id=...)` 收敛本进程不再持有的运行中记录；
   - 事务内 `SELECT ... FOR UPDATE` 锁定 `conversation` 行（锁顺序固定：会话 → Run）；
   - 幂等去重 → 冲突检查 → 创建 `Message` + `AgentInvocation` + `AgentRunExecution` + `agent_run` 行；
   - **提交前**先 `tracker.register(run)`（否则同时刻的其他请求会把这条新运行中记录判为孤儿），提交成功后才 `self._submit_execution(run)`；
   - 事务异常时 `tracker.finish(run)` 兜底注销。
2. **装配**（`AgentFactory.create_executor`）
   - 读应用定义 → 校验指纹 → 解析 Skills 选择 → 解析并授权模型 → 组装工具 → 准备 SubAgents → 构造 `AgentRunContext` 与 `AgentConfig` → 交给 `AgentRuntimeAssembler`。
3. **执行**（`AgentRunService._execute_run`）
   - 后台线程进入独立 Flask app context 与独立 Session；
   - 先发 `lifecycle/running(preparing)`，恢复执行时补发 `values` 清空待填表单；
   - `AgentExecutor.execute()` 驱动 `graph.stream_events(...)`；
   - 事件经 `AgentEventNormalizer` → `AgentProtocolEventProjector` → `AgentRunEventBuffer` 分批落库并通知订阅者。
4. **结算（`_finalize_run`）**
   - 状态映射：`completed` / `failed` / `cancelled` / `waiting_input`；
   - 写入 `Message.answer`、`latency`、用量；写入 `state_snapshot`；
   - **先在业务事务内生成 `values` → `input.requested` → `lifecycle` 事件，再提交**；
   - 提交成功后推进本地事件游标并通知 SSE 订阅者；通知失败只记日志，不改写已提交结果。
5. **终态之后**
   - `waiting_input`：等待 `input.respond`（提交答案续跑）或 `input.reopen`（恢复被取消的表单）。
   - 终态 Run 不可再被取消，也不再有执行权。

### 4.3 幂等与并发控制

四层保障：

1. **会话行锁**：`_lock_conversation()` 对 `conversation` 行 `FOR UPDATE`（同时校验 `app_id` 与 `is_deleted=false`），把去重、冲突检查、创建串行化。
2. **数据库部分唯一索引**（`internal/model/agent_run.py:65-84`）：
   - `agent_run_conversation_occupied_uq` on `(conversation_id) WHERE status IN ('running','waiting_input')`；
   - `agent_run_conversation_input_message_uq` on `(conversation_id, input_message_id) WHERE input_message_id IS NOT NULL`；
   - 另有 `uq_agent_run_message_id` 唯一约束（一条业务消息只承载一个 Run）。
3. **进程内注册表**：`AgentRunTracker` 按 `conversation_id` 保存本地 `AgentRunExecution`（`active_run_ids()` 供孤儿收敛使用），用于取消信号和"仍在运行"判断。
4. **输入消息幂等键**：同一 `input_message_id` 重复 `run.start` 返回**同一个 Run**，重放起点取该 Run 最小事件序号 - 1（从头重放；本进程仍持有该执行时改取内存游标，避免读到尚未提交的序号）；若该消息已用于其他入口，返回 `RunRejected("输入消息已用于其他调用入口")`。

启动冲突的提示（`_check_start_conflict`，`agent_run_service.py:424-469`）：
- `当前任务正在停止，请稍后重试`
- `请先完成当前表单`
- `当前任务已有正在执行的 Agent`
- 表单被取消后：`此任务已在表单处停止，请点击重新填写后继续，或创建新会话`

### 4.4 取消

`cancel_run`（`agent_run_service.py:309-341`）：

1. `get_conversation_run` 校验 Run 属于本次请求的应用、会话、入口（该方法是纯读，不触发任何写入）；
2. **归属确认之后**才 `_settle_orphan_runs(run_id=run_id)`，未授权请求不能触发写入；
3. 若 Run 非终态且 `_request_cancel(row)` 成功 → 通知 SSE + 通过进程内注册表发取消信号。

两条路径（`_request_cancel`，`agent_run_service.py:822-858`）：

- **执行中的 Run**：只写 `cancel_requested_at`（重复点击不刷新时刻），终态和执行权释放由执行线程负责。
- **等待表单的 Run**：`_cancel_pending_input` 在同一事务内写 `cancelled` / `finished_at`、清空 `executor_id`、把待填表单搬到 `cancelled_inputs` 并记录 `input_cancellation_sequence`、追加 `interrupted/cancelled` 事件、置 `Message.status = 'stop'`——**立即取消，没有执行线程参与**。

信号传递（`_watch_cancel`，`:1202-1214`）：
- 取消监控线程每 0.5 秒检查 `run.cancel_requested`（`threading.Event`），置位后调用 `agent.request_cancel()` 触发图排空。
- **取消完全依赖进程内信号**：数据库中的 `cancel_requested_at` 只作为业务事实记录，没有跨进程轮询。多进程部署时，A 进程发起的取消不会终止 B 进程持有的执行。

返回体表达**真实状态**：待填任务已 `cancelled`，执行中任务仍是 `running`。

### 4.5 孤儿 Run 收敛（`_settle_orphan_runs`）

`_settle_orphan_runs`（`agent_run_service.py:743-800`）是唯一的"卡住恢复"机制，取代了早期的租约/心跳方案：

- 判据：`status IN ('running')` **且** `id NOT IN alive_run_ids`（`alive_run_ids` = 本进程注册表中未 `done` 的 Run 标识，由 `AgentRunTracker.active_run_ids()` 提供）；
- 可选按 `conversation_id` 或 `run_id` 进一步收窄；
- 逐行 `with_for_update()`，`cancel_requested_at` 非空 → `cancelled`，否则 → `failed`；
- 公开错误文案统一为 `Agent 执行已中断，请重新发送消息。`；
- 清空 `executor_id`（撤销执行权，使迟到的事件和结果无法覆盖收敛后的状态），追加 `lifecycle` 事件，同步 `Message.status`（`stop` / `error`）。

触发时机：
- `start_run`（按会话，`agent_run_service.py:71`）；
- `cancel_run`（按 Run，`:266`，授权之后）；
- `get_state` 与 SSE `stream_events`（按会话 / 按 Run，`agent_run_query.py:56,78`）——重连能立刻读到真实终态；
- 内部 SDK `get_run`（按 Run，`internal_agent_protocol/service.py:181`，授权之后）。

**注意**：判据是"本进程是否持有"，因此该机制隐含**单进程部署**假设。多进程（多 worker / 多容器）场景下，进程 B 处理任意状态查询都会把进程 A 正在执行的 Run 判为孤儿并写成 `failed`。这是相对早期心跳方案的行为变化，需要在部署形态上确认。

### 4.6 用户提问（waiting_input）

- 工具：`ask_user`（`core/agent_v2/tools/ask_user.py`），基于 LangGraph `interrupt()`，**不占用线程等待**。
- 暴露条件：`AgentInvocation.user_input_enabled and app_config.user_input.enable`，并且必须存在 checkpointer。
- 表单能力（`core/agent_v2/entity/user_input.py`）：`text / textarea / select / multiselect / number`；每题最多一个推荐选项；字段级开关 `allow_other / allow_skip / allow_unavailable / allow_delegate`。
- 非答案状态语义：`skipped` / `unavailable` 表示未取得信息，`delegate` 仅授权代选该字段，**均不构成敏感操作授权**。
- 提交：`input.respond` 必须**一次覆盖全部待填表单**，`{interrupt_id: {字段: 值}}`；批次的 `input_batch_id` = 中断 ID 集合排序后的 sha256（与答案值和键顺序无关），保证提交幂等；同批次不同答案会被拒绝（`该表单已提交，不能重复修改`）。
- 恢复：表单在取消时被保留，`input.reopen` 用 `cancellation_sequence` 限定恢复批次；迟到的重复请求返回首次结果（`reopened_sequence`）。
- **配置指纹**：创建 Run 时计算应用定义指纹（`model_mode` 除外）；提交答案、恢复表单、装配时都会重新计算并比对，不一致则拒绝继续旧任务（`应用配置已变更，请恢复原配置后继续填写，或创建新会话`）。
- 已确认答案会在后续每次模型调用前作为 `HumanMessage` 重新注入（`RuntimeContextMiddleware`），并明确声明为用户级数据而非系统指令。

### 4.7 事件流与断线续传

- **事件通道**：`messages`、`values`、`tools`、`lifecycle`、`custom`，另有业务侧生成的 `input.requested`。
- **序号**：`event_id = "<run_id>:<seq>"`，`seq` 单调递增且以「会话 + 入口」为粒度续接（新建 Run 的起点是该会话该入口历史最大 `last_event_sequence`），因此跨 Run 不断号。
- **续传**：SSE 支持 `Last-Event-ID` 头与请求体 `since`（取 `max`），`<run_id>:cursor-reset:<n>` 形式解析为重放起点 `n-1`。
- **订阅过滤**：`channels` / `namespaces`（前缀匹配）/ `depth`；`input` 通道映射到 `input.requested`；兼容 LangGraph SDK 的 `checkpoints` channel（当前不发布）。
- **心跳**：15 秒一次 `: heartbeat` 注释行，不进入业务事件流。
- **提交先于通知**：任何事件先落库提交，再通过进程内版本号唤醒订阅者；订阅者的等待超时为 0.5 秒兜底轮询。
- **根终态由业务事务生成**：内核的根 `lifecycle` 终态事件会被投影器丢弃，改由业务提交后生成，避免"A 图已结束但业务未提交"的错序。
- **事件合并**：缓冲区会把同一 namespace/node 的相邻同类增量（text-delta / reasoning-delta）合并成一条，沿用最后一个序号——因此 **`seq` 允许跳号**，客户端必须用 `seq` 而非「+1」推进游标。
- **输出审核开启时的延迟投递**：`review_config.outputs_config` 生效时，整轮事件先缓冲，只有在成功完成时才回放；失败或取消则丢弃，避免未过审内容进入历史。

---

## 5. 执行链路

### 5.1 业务侧装配（`AgentFactory`）

`create_executor(req, observer=observer)` 的步骤：

1. **读定义**：`AgentDefinitionService.load()` → `ApplicationAgentDefinition(name, description, account_id, config)`。
2. **指纹复核**（仅当 invocation 带指纹）。
3. **Skill 选择校验**：`selected_skills` 必须是已挂载且 `enabled` 的 Skill 子集（空表示使用全部已启用 Skill）。
4. **模型解析**：
   - `ASSISTANT_AGENT` → 账号默认模型；
   - 其他 → 应用配置模型，缺失即 `Agent 运行配置缺少模型`；
   - 会话级覆盖：`model_service.resolve(...)`；
   - 思考强度覆盖：`with_reasoning_override`（先剥离 `reasoning_effort / thinking_level / thinking` 别名再写入）。
5. **工具组装**：
   - 助手 Agent → 内置 4 个 Creator 工具（`create_app`、`workflow_generator`、`app_with_workflow_generator` + 知识库检索）；
   - 其他 → 插件工具 + （有知识库时）检索工具 + （有工作流时）工作流工具。
6. **SubAgent 准备**（见 §6.3）。
7. **上下文构造**：`principal_id = end_user_id or created_by`；`memory_enabled = long_term_memory.enable`；`user_input_enabled = invocation.user_input_enabled AND app_config.user_input.enable`；`confirmed_user_inputs` 从已提交表单渲染。
8. **Workspace Store**：使用**独立 Session**（`expire_on_commit=False`）构造带授权范围的 `WorkspaceFileRepository` + `AgentWorkspaceStore`，任一异常即关闭 Session。

### 5.2 内核装配（`AgentRuntimeAssembler`）

`AgentRuntimeAssembler(config, workspace_store).build(context, definition, observer, input_files)`：

1. 建 **Workspace 投影**：`WorkspaceProjection` 提供 `TemporaryDirectory(prefix="my-dify-workspace-")` 下的虚拟模式文件后端（本轮工作副本）。
2. 建 **Skill 投影**：`build_skill_context` 在临时目录里按 `/<skill 名>/` 落盘授权 Skill 包，产出路由与沙箱种子文件。
3. 建 **记忆路由**：`memory_enabled` 时要求 Store 必须存在（否则 `RuntimeError`），把 `/memories/` 路由到 `AgentMemoryBackend`（命名空间由可信上下文解析）。
4. 建 **沙箱后端**：按 `AGENT_SANDBOX_PROVIDER` 选择；沙箱名 = `mydify-sandbox-<sha256('mydify:{account}:{app}:{conversation}:{agent_id}')[:32]>`。
5. 组装 **CompositeBackend**：
   - 无沙箱 → 默认 `StateBackend`，路由 `/workspace/`、Skills、Memory；
   - 有沙箱 → 默认沙箱，路由 Skills、Memory、`/` → 沙箱（DeepAgents 0.6 的权限编译要求显式根路由）。
6. 产出 `AgentRuntimeResources`：checkpointer、backend、workspace、workspace_store、文件交付、文件权限、Skill 投影、记忆来源（`/memories/.index.md`）、运行上下文。
7. **附件写入**：`runtime.backend.upload_files(input_files)`，数量不符或任一项报错即 `WorkspaceDeliveryError("附件写入 Agent Workspace 失败")`。
8. 建图并构造 `AgentExecutor`；任一步失败都会关闭已建资源并重新抛出。

### 5.3 建图（`build_deep_agent`）

- 工具 = 应用授权工具（经保留名校验） + 三个交付工具：`list_saved_files` / `load_saved_file` / `deliver_file`。
- 系统提示词 = `BASE_AGENT_PROMPT` 模板（应用说明 / 业务指令 / 任务进度沟通 / 文件路径约束） + 记忆指引 + 提问指引。
- `memory=None`：长期记忆由 `ReloadingMemoryMiddleware` 自行装配，避免官方 MemoryMiddleware 重复注入。
- 子 Agent 列表 = 配置顺序；当执行器使用虚拟路径且未显式声明 `general-purpose` 时，在**索引 0** 注入框架的 `general-purpose`，并补挂 `ShellExecutionMiddleware(virtual_paths=True)`。
- `create_deep_agent(..., skills=[...] or None, permissions=..., backend=..., checkpointer=..., store=..., context_schema=AgentRunContext)`。

### 5.4 平台中间件顺序

`assembly/graph/platform_middleware.py`（顺序即包装顺序）：

1. `ReloadingSkillsMiddleware`（每轮重新枚举 Skill 目录，避免 checkpoint 泄漏旧选择）
2. `ToolCallValidationMiddleware`（校验模型输出的工具调用结构）
3. `ShellExecutionMiddleware(virtual_paths=...)`（虚拟路径说明；按需移除/拒绝 `execute`）
4. `RuntimeContextMiddleware`（注入规划块与已确认答案）
5. `ReviewMiddleware(review_config)`（关键词入/出审核）
6. `ModelRetryMiddleware(max_retries=2, on_failure="error")`
7. `ToolRetryMiddleware(max_retries=2, tools=("ls","read_file","glob","grep"), retry_on=(TimeoutError, ConnectionError))`
8. `ModelCallLimitMiddleware(run_limit=50, exit_behavior="end")`
9. `ToolCallLimitMiddleware(run_limit=200, exit_behavior="continue")`
10. `ReloadingMemoryMiddleware`（仅启用长期记忆时）
11. `ContextWindowMiddleware`（上下文窗口/压缩观测）
12. `UserInputMiddleware`（仅启用提问且有 checkpointer 时，暴露 `ask_user`）

可重试的模型状态码：`408 / 409 / 429` 与 5xx，另加 `TimeoutError` / `ConnectionError` / `APIConnectionError`。

### 5.5 执行器（`AgentExecutor`）

- 单次执行语义：一个 executor 只能 `execute` 一次（`AgentExecutor 只允许执行一次`）。
- 输入二选一：`message`（新任务）或 `resume`（中断续跑），二者必须且只能提供一个。
- 图调用：`graph.stream_events(input, config, context, version="v3", control=run_control)`；`config.configurable.thread_id = conversation_id`。
- 回调：`PlatformAgentRunObserver`（用量与调用计数）、`UserInputInterruptTracker`（识别提问中断）、`SummaryProgressCallback`（摘要观测）。
- 协作式取消：设置内部取消事件并 `control.request_drain("cancel_requested")`；取消后停止发布事件但继续抽干图。
- 结果状态优先级：**failed > cancelled > waiting_input > completed**。
- 输出审核开启且（出错或取消）时，`state.messages` 会被剔除，未过审内容不进入业务历史。
- **提问中断的 `tool-error` 被过滤**，避免把"等待用户输入"当成工具失败。
- 资源在 `execute` 内总是被关闭；关闭失败会被记录为终态 `CLOSE_FAILED` 并在后续调用中持续可见。

### 5.6 文件交付模型

三类空间：

| 路径 | 语义 | 生命周期 |
| --- | --- | --- |
| `/workspace/`（本轮） | 临时工作副本 | 随 Run 结束销毁 |
| `/memories/` | 长期记忆 | 跨会话持久化在 LangGraph Store |
| `/skills/`、`/subagent_skills/<name>/` | 只读 Skill 包 | 每轮从平台重新投影 |

Agent 只能通过三个工具把产物带出临时空间（`execution/workspace_delivery.py`）：

- `list_saved_files(prefix)` —— 列出已保存文件（仅元数据）；
- `load_saved_file(path, destination)` —— 把已保存文件取回工作副本；同名冲突时提示换 destination；
- `deliver_file(file_path, destination)` —— 把工作副本文件落库为持久产物。

交付使用**乐观并发**：`expected_revision` 为 0 表示只允许新建，因此覆盖已保存文件必须先在本轮 `load_saved_file`。

文件权限（`assembly/context/sandbox_context.py`，按顺序判定）：
- 拒绝写 `/skills`、`/subagent_skills`；
- 允许读 `/`、Skills、`/workspace`；
- 允许写 `/workspace`；
- `/memories` 按 `memory_enabled` 允许或拒绝；
- 最后 `/**` 读写全拒绝兜底。

---

## 6. Agent 运行时能力

### 6.1 模型与会话级覆盖

- 可切换模型的入口只有：`debugger`、`creator_task`、`assistant_agent`（`_MODEL_SELECTABLE_SOURCES`）；`service_api` 与 `web_app` **不允许**切换。
- 选择只能引用模型：`AgentModelSelection{provider, model, provider_connection_id}`，不接受凭证、地址或任意参数。
- 归属约束：`owner_account_id == account_id` 且 `created_by == owner_account_id`，否则 `只能选择当前账号的模型`。
- 模型必须已启用且是对话模型（`ModelType.CHAT`），否则 `所选模型未激活或不是对话模型`。
- 思考强度校验按模型声明的 `reasoning_effort` 参数**选项集合**判定，不能仅凭参数名接受（`当前模型不支持所选思考强度，请重新选择`）。
- 会话级「上次使用的模型/思考强度」不单独建表，而是从该会话该入口**最新 Run 的 invocation 快照**读取三个字段（`model_selection` / `model_selection_explicit` / `reasoning_effort`，见 `run_service.get_latest_model_overrides`）；`model_selection_explicit` 标记用于避免旧客户端回传默认模型造成永久绑定。

### 6.2 工具

- 插件工具绑定 ≤5，不允许重复 (provider_id, tool_id)。
- 校验时若参数键与工具定义不符，会回退到工具默认参数（`_process_and_validate_tools`）。
- 工具名不能与 DeepAgents 内置名冲突（保留名集合：`task, write_todos, ls, read_file, write_file, edit_file, glob, grep, execute, delete_memory_file, ask_user, list_saved_files, load_saved_file, deliver_file`）。

### 6.3 SubAgent

配置实体 `SubAgentConfig`（`entity/subagent_config.py`）关键约束：

| 字段 | 取值 / 约束 |
| --- | --- |
| `name` | 1–64，禁止 `.`/`..`、控制字符、`/\:*?"<>|`；同一应用内不可重名 |
| `description` | 1–500 |
| `system_prompt` | ≤8000（仅同步自定义） |
| `tools` / `skills` | 名称列表，不可重复 |
| `mode` | `sync` / `async` |
| `source` | `custom` / `published` |
| `context_passing` | 固定 `NONE` |
| `model_mode` | `inherit` / `custom`（仅同步自定义） |
| `graph_id` | ≤255；发布异步必须等于 `mydify:published:<published_app_id>` |
| `published_app_id` | 发布来源必填 |
| `headers` | ≤32 项，名 ≤128，值 ≤2048 |

行为差异：

- **同步自定义**：可指定 `system_prompt`、工具、Skills、可选独立模型（`model_mode=custom`）；工具必须已在主 Agent 授权集合内，否则 `subagent '<name>' 引用了未授权工具`；`model_mode=inherit` 时继承主 Agent 模型。
- **同步发布**：使用目标已发布应用的模型与工具，系统提示词由平台合成（`你是已发布应用「…」的执行 Agent…` + 目标 `preset_prompt`）；不支持独立模型覆盖。
- **异步（含发布）**：通过 Agent Protocol 后台任务执行，只携带 `name/description/graph_id/url/headers`；发布来源由平台签发内部凭证并解析 transport；异步不支持 `system_prompt` / `tools` / `skills` / 模型覆盖。
- 跨字段规则：**SubAgent 只能选择主 Agent 已挂载且启用的 Skills**。
- 发布引用校验：租户边界、目标必须 `published`、引用环检测、嵌套深度 ≤32、禁止自引用。
- 运行时权限：SubAgent 使用**替换**的权限规则（只有自己的 Skill 源、`/workspace`，以及最后全拒绝兜底），**完全看不到 `/memories/`**。
- SubAgent 的 `execute` 一律关闭（文件权限约束不了任意命令），命中时返回错误 `ToolMessage` 而非抛异常。

### 6.4 Skills

- 来源：`BUILTIN_SKILLS_ROOT`（默认 `resources/agent_skills/builtin`）下的目录，每个含 `SKILL.md`（frontmatter：`name`、`description`、`metadata.my_dify.{version,category,execution_mode,requires}`、`allowed-tools`）。
- 分类标签：`office / content / knowledge / data / education / agent`。
- 执行模式：`inline` / `sandbox`。
- 应用绑定 `{name, version, digest, enabled}`：名称需匹配 `^[a-z][a-z0-9-]*$`，同一应用不可重复；校验时若未指定版本则取目录版本，**版本必须与目录一致**，否则 `Skill 版本不匹配`；`digest` 由目录刷新写入（客户端传值被忽略）。
- 每轮运行可传 `selected_skills`（≤20）做本轮选择，必须是已启用子集。
- 投影：主 Agent Skill → `/skills/`；同步 SubAgent Skill → `/subagent_skills/<name>/`；沙箱模式下同一份字节通过 `seed_sandbox_skills` 上传（≥64 KiB 的包会先压缩，压缩后不超过原文 90% 才使用压缩）。
- `ReloadingSkillsMiddleware` 每轮重新枚举目录，防止上一轮的 Skill 选择或版本从 checkpoint 泄漏。

### 6.5 长期记忆

- 隔离身份：`AgentMemoryIdentity(account_id, app_id)`，LangGraph Store 命名空间 `("mydify","agent-memory",<account_id>,<app_id>)`。
  命名空间**不含终端用户维度**，因此同一应用下的所有终端用户共享一份记忆（见 §12 第 12 条）。
- 虚拟路径：`/memories/`；分类固定为 `preferences`（用户偏好）、`background`（背景信息）、`experience`（经验方法）。
- 索引 `/memories/.index.md` 由系统生成（**只读**，写入会被拒绝：`记忆索引由系统生成，请编辑分类下的记忆文件`）。
- 配额：单文件 64 KiB、文件数 100、总容量 1 MiB；路径长度 ≤512，路径片段禁止空、`.`、`..`、`~` 与控制字符。
- 写入原子性：在同一作用域锁内完成"读 → 校验 → 提交"；Postgres Store 下使用 `set_config('lock_timeout','5000')` + `pg_advisory_xact_lock`。
- 索引渲染预算：目录行 3500 字节、正文 3600 字节、单条偏好 900 字节，超出提示 `其余文件使用 ls /memories/ 查看。`
- Agent 侧工具：`edit_file` / `write_file` / `delete_memory_file`；`delete_memory_file` 由 `ReloadingMemoryMiddleware` 提供，并**要求 `context.agent_id == "main"`**，即 SubAgent 无记忆能力。
- 平台侧管理接口需要应用已启用长期记忆（`enable=true`），否则 `ForbiddenException("该应用未启用长期记忆")`。
- 删除应用时会清空该应用下所有主体的记忆。

### 6.6 Workspace

- 作用域：`build_workspace_scope_id(account_id, app_id) = uuid5(NAMESPACE_URL, "my-dify:agent-workspace:v1:{account}:{app}:{account}")`，**跨会话共享**，且不含终端用户维度。
- 因此删除单个会话**不会**删除共享文件（代码中显式注释说明）。
- 虚拟路径 `/workspace/...`，长度 ≤512；拒绝 `\`、NUL、控制字符、`.`/`..`/`~` 片段与作用域逃逸；存储路径 = `"{scope_id}/{relative}"`。
- 配额：`WORKSPACE_MAX_BYTES`（默认 100 MiB）、`WORKSPACE_MAX_FILES`（默认 1000）。
- 元数据表 `workspace_files`，内容存 COS；文件带 `revision` 与 `content_sha256` 用于并发校验。
- 下载走带 TTL 的签名 URL（默认 300 秒），平台接口以 302 跳转返回。

### 6.7 沙箱

`AGENT_SANDBOX_PROVIDER` 可取值与特性（`backends/sandbox/`）：

| provider | 说明 |
| --- | --- |
| `disabled`（默认，别名 `none/off/false`） | 无沙箱，使用 `StateBackend` |
| `bubblewrap`（别名 `bwrap`） | 本机 bwrap 隔离；默认 `--unshare-net`；`/usr /bin /lib /lib64 /sbin /etc` 只读；超时退出码 124；**不继承宿主环境变量** |
| `local_shell` | 本机 shell（`inherit_env=True`）；**生产环境（`FLASK_ENV=production`）被拒绝** |
| `daytona` | 远程沙箱，需 `DAYTONA_API_KEY`；进程内租约池 `max_idle=3` |
| `langsmith` | 远程沙箱，支持 `snapshot_id`；空闲 TTL 与停止后删除 TTL 可配 |

公共约束：执行超时默认 1800 秒，单次输出上限默认 500 KiB；沙箱名为账号/应用/会话/agent 的哈希，同名复用；`AGENT_SANDBOX_SNAPSHOT_ID` 只在 langsmith 与 daytona 下合法，其他 provider 配置会报错（bubblewrap/local_shell 与 daytona 均显式拒绝）。

### 6.8 审核

- 结构：总开关 + 关键词表 + 输入审核（含预设回复）+ 输出审核；子配置只在总开关开启时生效。
- 关键词匹配使用字符级词边界正则 `(?<![A-Za-z0-9_])kw(?![A-Za-z0-9_])`，默认忽略大小写，兼容中文。
- 输入命中：`before_model` 检查最后一条 `human/user` 消息，命中则跳转到结束并返回预设回复（默认 `输入内容未通过审核。`）。
- 输出命中：`wrap_model_call` 对每条响应消息 `check_output`，命中抛 `ReviewBlocked("输出内容未通过审核。")`，Run 记为 `failed`。
- 输出审核开启时事件整轮缓冲，未成功完成不回放，`state.messages` 也被剔除。
- 注意：输入审核只看最后一条消息，历史轮次或工具消息中的关键词不会被拦截。

### 6.9 上下文窗口观测

- `ContextWindowMiddleware` 在每次模型调用前后通过 `stream_writer` 发出自定义事件：
  - `context_window`：分为 `system / memory / human / assistant / tool / tools / summary` 七类计数，含 `context_limit`、`limit_kind`、`output_reserve`、`estimated_input_tokens`、`actual_input_tokens` 等；文本 token 估算为 `ceil(utf8字节数 / 2)`，每条消息 +4。
  - `context_compaction`：摘要生成阶段观测（`preparing / summarizing / completed / failed / cancelled`）。
- `custom` 通道对协议**白名单**：只有上述两种结构会进入公开载荷，其余自定义数据被丢弃。

### 6.10 助手 Agent（Creator 内置）

- 应用 ID 来自 `ASSISTANT_AGENT_ID`；**不要求存在 App 记录**。
- 每个账号复用**同一个**会话（`get_or_create_assistant_conversation`）。
- 工具绑定账号闭包（不持有 ORM 或 Flask 上下文）：知识库检索 + `create_app` + `workflow_generator` + `app_with_workflow_generator`，后三者通过 Celery 异步任务真正创建资源。

---

## 7. 事件与状态投影

### 7.1 内核 → 协议

`service/agent_protocol/event_projector.py`

- channel 到 `method` 基本 1:1；根终态 lifecycle 被丢弃（由业务事务生成），**带 namespace 的子 Agent 终态不被丢弃**。
- `values` 经过 `project_public_state` 裁剪：只保留 `todos / files / async_tasks / pending_inputs / input_history / input_recovery`，并把 `async_tasks[*].thread_id` 改名 `conversation_id`。
- `messages` 的根 `message-start`（ai/assistant）会被绑定为 `"<message_id>:assistant"`。
- `custom` 白名单校验（见 §6.9）。
- 元数据附带 `metadata.my_dify = {source: agent_v2|workbench, source_seq}`。

### 7.2 `messages` 通道事件名

`execution/event_normalizer.py` 产出：`message-start`、`content-block-start`、`content-block-delta`、`content-block-finish`、`message-finish`。只投影 `text` 与 `reasoning` 两种内容块；工具参数与签名不下发。摘要来源（`lc_source == "summarization"`）的消息被丢弃。

### 7.3 `tools` 通道事件名

`tool-started`、`tool-output-delta`、`tool-finished`、`tool-error`（以 `tool_call_id` 关联）。

### 7.4 状态查询（`/state`）

- `values`：公开状态 + `messages`（业务历史，最近 20 条，含 `message_page{has_more,next_before}`）+ `runs`（会话内 Run 摘要列表，`list_state_runs` 一次查询同时覆盖消息页关联 Run 与当前入口最新 Run）。
- `next`：仅当当前 Run 为 `running` 时返回 `["agent"]`（`waiting_input` 返回空）。
- `tasks`：仅 `waiting_input` 时返回，形如 `{id, name: "ask_user", interrupts: [{id, value, ns}]}`。
- `metadata`：`{run_id, status, last_event_sequence}`；无 Run 时为 `{null, "idle", 0}`。
- 活动 Run 的快照会补齐尚未落库的消息，但只允许覆盖本轮的消息标识。

### 7.5 历史消息形状

`WorkbenchHistoryMessageResp{id, type: "human"|"ai", content, additional_kwargs}`：

- human：`id = input_message_id 或 "<message_id>:human"`，`additional_kwargs = {business_message_id, run_id, image_urls, attachments}`；
- ai：`id = "<message_id>:assistant"`，`additional_kwargs = {run_id, metrics: {latency, total_token_count, input_token_count, output_token_count, cached_token_count, reasoning_token_count}}`；仅当 `answer` 非空才存在。

---

## 8. 数据模型

### 8.1 `agent_run`（`internal/model/agent_run.py:28`）

| 列 | 类型 | 说明 |
| --- | --- | --- |
| `id` | UUID PK | |
| `app_id` / `conversation_id` | UUID | 无外键（内置助手不需要 App 记录） |
| `invoke_from` | String(32) | 入口隔离键 |
| `message_id` | UUID FK→`message.id` CASCADE，唯一 | 承载问答与用量投影 |
| `input_message_id` | String(255) | 客户端幂等键 |
| `status` | String(32) | `running/waiting_input/completed/failed/cancelled` |
| `error_message` | Text | 面向用户的失败原因 |
| `invocation_snapshot` | JSONB | 恢复参数与定义指纹 |
| `interaction_state` | JSONB | 待填/已确认/已取消表单状态 |
| `state_snapshot` | JSONB | 展示用状态投影 |
| `last_event_sequence` | Integer | 事件续传游标 |
| `executor_id` | UUID nullable | **本次执行段标识**（不是账号/进程 ID）；启动或恢复换新，结算清空 |
| `cancel_requested_at` | DateTime | 取消请求时间戳；终态保留原值供追溯 |
| `started_at` / `finished_at` | DateTime | |

约束要点：状态枚举 CHECK；执行权 CHECK（`running` 必有 `executor_id`，其余状态必须为空）；`finished_at` 与终态的对应 CHECK；三个 JSONB 必须是 object；`(conversation_id, created_at, id)` 普通索引；两个部分唯一索引 + `message_id` 唯一约束（见 §4.3）。

`public_snapshot` 属性把 `state_snapshot` 与 `interaction_state` 中的 `pending_inputs / input_history / input_recovery` 合并成公开投影；`apply_snapshot` 反向拆分，禁止公开事件覆盖私有恢复参数（`_` 前缀键被丢弃）。

### 8.2 `agent_run_submission`

PK `(run_id, input_batch_id)`；`input_batch_id` 是中断 ID 集合的 sha256（**不是**答案哈希）；`accepted_sequence` 记录首次接受的事件序号，用于提交幂等。

### 8.3 `agent_run_event`

PK `(run_id, sequence)`，CHECK `sequence > 0`，`payload` 为 object 的 `WorkbenchEvent` JSON。写入走 `ON CONFLICT DO NOTHING`（按 `(run_id, sequence)` 去重）。

### 8.4 `agent_evaluation`

`id / account_id / app_id / run_id / source_snapshot / criteria / judge_config / evaluator_version / status / result / error_message / created_at / finished_at`；索引 `(account_id, app_id, created_at, id)`。

### 8.5 `workspace_files`

`id / account_id / app_id / conversation_id / path / object_key / mime_type / size_bytes / content_sha256 / revision / created_by / is_deleted / created_at / updated_at`；
唯一约束 `(account_id, app_id, path)` 与 `object_key`。
**注意**：`conversation_id` 是历史列名，当前存的是账号+应用派生的 Workspace `scope_id`，**不是** `Conversation.id`。

### 8.6 LangGraph 托管表

- Checkpoint：`PostgresSaver`（`ResilientPostgresSaver` 对幂等操作补一次瞬时断连重试），`setup()` 幂等建表；`thread_id = conversation_id`。
- Store：`PostgresStore`（`ResilientPostgresStore`），用于长期记忆文件。
- 连接池：checkpoint 默认 `min=1/max=5`，memory 默认 `min=1/max=2`，均为进程级长期存活，`atexit` 注册关闭。
- 会话删除 / 消息删除 / 应用删除都会触发 `delete_agent_checkpoint(conversation_id)`。

---

## 9. 并发、资源与限制

| 项 | 值 | 位置 |
| --- | --- | --- |
| Agent 执行线程池 | 4（另有等量 `BoundedSemaphore(4)` 准入） | `agent_run_service.py:55-57` |
| 执行繁忙提示 | `当前 Agent 执行繁忙，请稍后重试` | `:69` |
| 事件批量提交 | 8 条 / 0.1 秒（无定时线程，仅在事件到达时判断） | `agent_run_events.py:75-78` |
| 取消信号轮询 | 0.5 秒（只认进程内 `threading.Event`） | `_watch_cancel` |
| 取消监控 join 上限 | 1 秒（超时只告警，不阻塞落库） | `:209-211` |
| SSE 心跳 | 15 秒 | 查询服务 |
| SSE 兜底轮询 | 0.5 秒 | 查询服务 |
| `replace_run` 等待前序停止 | 10 秒（0.1 秒步进），超时返回可重试的 `RunRejected` | `agent_run_service.py:131-145` |
| 模型调用上限 | 50（达限结束） | `platform_middleware.py` |
| 工具调用上限 | 200（达限继续） | 同上 |
| 模型/工具重试 | 各 2 次 | 同上 |
| 提问表单 | ≤8 题、每题 ≤20 选项、标题 ≤200、描述 ≤1000、文本 ≤4000 字、数值 ±1e15 | `entity/user_input.py` |
| 已确认答案回注预算 | 12000 字符（渲染预算 11500、≤48 条） | `entity/run_context.py`、`entity/user_input.py` |
| 已确认答案读取批 | 同一会话同一入口最近 8 条 Run | `run_service.get_confirmed_input_context` |
| 图片 / 附件 | 每次 ≤10 张图片、≤5 个附件，均不可重复 | `schema/agent/agent_workbench_schema.py` |
| 本轮 Skill 选择 | ≤20 个 | 同上 |
| 记忆配额 | 64 KiB/文件、100 文件、1 MiB 总量 | `entity/agent_memory.py` |
| Workspace 配额 | 100 MiB / 1000 文件（可配） | `config/default_config.py` |
| 沙箱执行 | 超时 1800 秒、输出 500 KiB | 同上 |
| 滚动事件续传页 | 256 条/批 | `agent_run_service.py:531` |
| 历史 Run 事件分页 | 默认 20/页，最大 50 | `pkg/paginator` |
| 状态历史消息 | 固定最近 20 条 | 查询服务 |

**执行槽归还**：`_execute_background_run` 的 `finally` 中 `db.session.remove()` 并 `_run_slots.release()`；`launch` 提交失败时归还本次已占用的槽。

---

## 10. 配置项清单

### 10.1 运行时后端

| 配置 | 默认 | 可选值 / 约束 |
| --- | --- | --- |
| `AGENT_CHECKPOINT_BACKEND` | `postgres` | `postgres` / `memory` / `disabled`(别名 `false/off/none`)；生产必须是 `postgres` |
| `AGENT_CHECKPOINT_POOL_MIN_SIZE` / `MAX_SIZE` / `TIMEOUT_SECONDS` / `MAX_WAITING` | 1 / 5 / 30 / 0 | min≥0，max≥min 且 >0，timeout>0，max_waiting≥0 |
| `AGENT_MEMORY_BACKEND` | `postgres` | `postgres` / `memory`；**`disabled` 被显式拒绝**；生产必须是 `postgres` |
| `AGENT_MEMORY_POOL_MIN_SIZE` / `MAX_SIZE` / `TIMEOUT_SECONDS` / `MAX_WAITING` | 1 / 2 / 30 / 0 | 同上 |

### 10.2 沙箱

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `AGENT_SANDBOX_PROVIDER` | `disabled` | `disabled` / `bubblewrap`(别名 `bwrap`) / `langsmith` / `daytona` / `local_shell` |
| `AGENT_SANDBOX_SNAPSHOT_ID` | `""` | 仅 langsmith / daytona 允许 |
| `AGENT_SANDBOX_IDLE_TTL_SECONDS` | 600 | 空闲回收；0 或 60 的倍数 |
| `AGENT_SANDBOX_DELETE_AFTER_STOP_SECONDS` | 86400 | 停止后删除；0 或 60 的倍数 |
| `AGENT_SANDBOX_ALLOW_NETWORK` | `False` | bwrap 是否放开网络 |
| `AGENT_SANDBOX_EXECUTE_TIMEOUT_SECONDS` | 1800 | >0 |
| `AGENT_SANDBOX_MAX_OUTPUT_BYTES` | 512000 | >0 |
| `DAYTONA_API_KEY` / `DAYTONA_API_URL` | `""` / `https://app.daytona.io/api` | 生产启用 daytona 时必须配置 Key |

### 10.3 资源与协议

| 配置 | 默认 |
| --- | --- |
| `BUILTIN_SKILLS_ROOT` | `resources/agent_skills/builtin` |
| `WORKSPACE_MAX_BYTES` / `WORKSPACE_MAX_FILES` | 104857600 / 1000（生产必须显式配置） |
| `WORKSPACE_PREVIEW_MAX_BYTES` | 1048576 |
| `WORKSPACE_DOWNLOAD_URL_TTL_SECONDS` | 300 |
| `AGENT_INTERNAL_PROTOCOL_BASE_URL` | `http://127.0.0.1:5000/agent-protocol` |
| `AGENT_INTERNAL_PROTOCOL_SECRET` | `""`（生产必须配置，回退 `JWT_SECRET_KEY`） |
| `AGENT_INTERNAL_PROTOCOL_TOKEN_TTL_SECONDS` | 86400（≥60） |
| `ASSISTANT_AGENT_ID` | `6774fcef-b594-8008-b30c-a05b8190afe6` |

### 10.4 后台任务

Agent 相关工作台侧**没有定时任务**：孤儿 Run 只在读取/启动/取消路径上被动收敛（§4.5）。唯一的 Agent 后台任务是测评派发（`internal/task/agent_evaluation_task.py`）。

---

## 11. 测评（Agent Evaluation）

对**已经结束的历史任务**做独立评分，不介入生产 Agent 图。

- 来源：必须是应用所有者自己的、已结束（`completed/failed/cancelled`）的调试/任务记录；列表、详情、提交使用同一权限条件。
- 材料冻结：提交时把 `query` 与 `answer` 快照写入 `source_snapshot`，之后修改应用不改写历史结论；`query + answer` 超过 48000 字直接拒绝（`不会截断后评分`）。
- 举证范围声明：`仅本轮任务描述与最终回答；不包含此前对话、附件内容、工具轨迹及外部系统核验。`
- 评分标准：1–12 条、每条 ≤500 字、不允许重复。
- 判分模型：只能引用已有模型连接（`AgentModelSelection`），提交时即校验可加载。
- 确定性核验规则 `evidence_rules`（≤8 条）：`contains` / `json_keys` / `file_exists`，其中 `file_exists` 只接受虚拟空间绝对路径。
- 两种模式：`historical`（对历史回答评分）与 `execute`（在隔离的临时环境里按 rubric 重新执行）；`execute` 使用**草稿配置**的模型、`preset_prompt` 与 `review_config`。
- 执行模式的图：`StateBackend` + `InMemorySaver`，`recursion_limit=150`，模型调用上限 30、工具调用上限 50，`exit_behavior="error"`，工具白名单 `ls/read_file/write_file/edit_file/glob/grep/write_todos`。
- 接受判定：完整评分要求**最后一轮** `status == satisfied`、该轮候选哈希与最终状态哈希一致、且证据存在，否则降级为 `grader_error`。
- 幂等：客户端可传 `request_id`，服务端按 `uuid5("evaluation:{account}:{app}:{request_id}")` 生成 ID；重复提交复用同一条记录（内容不一致则报错）。
- 截止时间：900 秒；超时的排队/运行中记录被收敛为超时错误。
- 异步：提交写 `queued` 后投递 Celery 任务（`expires=900`，`historical` soft 240 / hard 270 秒，`execute` soft 720 / hard 750 秒），HTTP 不等待评分。
- 状态：`queued` / `running` / 终态（`completed` 或 `error`）；历史列表省略长文本与逐轮候选，详情接口返回完整内容。

---

## 12. 阅读代码时发现的问题（未修改）

以下为当前实现的客观事实，仅作提醒：

1. ~~**`AGENT_RUN_OCCUPYING_STATUSES` 未导入**~~（已修复）
   原实现中 `_check_start_conflict` 与 `_request_cancel` 引用了该名字但模块顶部未导入，运行到这两行会抛 `NameError`，影响 `start_run` → `_check_start_conflict` 与 `cancel_run` → `_request_cancel` 两条路径。
   现已补上导入，并对 `internal/`、`app/` 全量做了未定义全局名扫描，确认无同类问题。

2. **孤儿收敛隐含单进程部署假设**
   `_settle_orphan_runs` 的判据是"不在本进程注册表中"，而它被 `get_state` / SSE `stream_events` / `start_run` / `cancel_run` 主动调用。多 worker 或多容器部署时，任一进程处理状态查询都会把其他进程正在执行的 Run 写成 `failed`。取消同理：`_watch_cancel` 只读进程内 `threading.Event`，跨进程的 `cancel_requested_at` 不会终止执行。

3. **`agent_run` 的 ORM 与迁移链双向不一致**（`internal/model/agent_run.py` vs `internal/migration/versions/`）
   - ORM 声明了 `invocation_snapshot`、`interaction_state`、`cancel_requested_at`，但**整条迁移链中没有任何一个迁移创建这三列**（全文检索确认）。
   - 反向地，迁移 `d3cecad0e5ba_` 为 `agent_run` 增加了 `heartbeat_at`，而当前 ORM 已不再使用它；该迁移只删了 `executor_id`/`heartbeat_at` 的 downgrade 分支，没有后续迁移回收。
   - 占位索引也不一致：迁移 `2add482a0d04_` 创建的是 `agent_run_conversation_executing_uq`（`status IN ('queued','running')`），ORM 声明的是 `agent_run_conversation_occupied_uq`（`status IN ('running','waiting_input')`）。
   按仓库规范，这里只报告差异与风险，不生成或修改任何迁移文件；实际数据库结构是否已补齐取决于用户自己执行过的迁移。

4. **交付工具未过保留名校验**（`assembly/graph/deep_agent_factory.py:32-34`）
   `validate_agent_tools(definition.tools)` 在追加 `list_saved_files / load_saved_file / deliver_file` **之前**执行，因此应用工具与这三个名字冲突时不会被拦截。

5. **提问中断的 namespace 被硬编码为空**（`execution/agent_executor.py:206`）
   `AgentEvent.namespace` 在别处是有值的，但收集 interrupt 时固定写入 `[]`，导致 `PendingUserInput.namespace` 恒为空。配合 `WorkbenchInputResponseReq.namespace` 的 `max_length=0`，子 Agent 的提问在当前协议下无法被回答。

6. **`ReviewMiddleware` 每轮构造两个实例**
   一个在图层里（`platform_middleware.py:60`）负责入/出审核，另一个由 `AgentExecutor.__init__` 持有（`agent_executor.py:59`）只用于事件延迟投递与结果裁剪。两者配置相同，但职责分散，阅读时容易误判。

7. **事件队列是进程内的**（`agent_run_events.py`）
   没有 Redis/pub-sub。多进程部署下，A 进程提交的事件不会唤醒 B 进程的订阅者，只能靠 0.5 秒兜底轮询收敛。

8. **`get_state` 的 `next` 不含 `waiting_input`**
   `waiting_input` 仍占用会话（`AGENT_RUN_OCCUPYING_STATUSES`），但 `next` 只在 `running` 时返回 `["agent"]`。客户端若只用 `next` 判断"是否忙碌"会与后端的占位规则不一致。内部 SDK 侧同样如此：`InternalThreadResp.status` 对 `waiting_input` 报 `idle`，而 `InternalRunResp.status` 对同一个 Run 报 `running`。

9. **状态查询固定读取最近 20 条消息**
   更早的分页只能靠 `values.message_page.next_before` 另行拉取。

10. **模型解析账号参数不对称**
    `restore_model_settings` 用 `owner_account_id` 解析模型实体（`agent_model_service.py:66`），`validate_reasoning_effort` 用 `account_id`（`:133`）。两者通常相同，但语义不完全一致。

11. **服务层没有 Run 级重试**
    失败 Run 不会自动重投；可重试的只有三种命令的幂等重放（`run.start` 去重、`input.respond` 批次幂等、`input.reopen` 批次幂等）与 `replace_run` 的 10 秒等待。

12. **`AgentRunContext.principal_id` 是死字段**（`core/agent_v2/entity/run_context.py:51`）
    装配时写入 `end_user_id or created_by`（`agent_factory.py:243`），但**全仓库再无任何读取点**。记忆命名空间只用 `(account_id, app_id)`（`entity/agent_memory.py:61-67`），Workspace 作用域同理。因此 WebApp / 开放 API 下的多个终端用户**共享同一份长期记忆与同一份 Workspace 文件**，并没有按终端用户隔离。

13. **`AgentRuntimeResources.close()` 会持续抛出已保存的异常**
    关闭失败后再次调用会重复抛出同一个 `BaseExceptionGroup`，`AgentExecutor` 也因此停留在 `CLOSE_FAILED`。这是有意的"失败可见"设计，但调用方必须能容忍重复异常。
