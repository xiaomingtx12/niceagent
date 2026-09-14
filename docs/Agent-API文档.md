# Agent API 文档

> 本文档依据当前代码实现整理（`internal/router`、`internal/handler`、`internal/schema`、`pkg/response`），不引用任何历史设计文档或仓库内 Markdown。
> 所有路径均为**根路径**（两个蓝图 `my_dify`、`openapi` 的 `url_prefix` 都是空串）。
> 标注 `【缺陷】` 的地方是当前实现的客观问题，未做修改。

---

## 1. 总览

### 1.1 响应约定（三种，务必区分）

Agent 相关接口分属三套响应风格，**不能混用判断逻辑**：

| 风格 | 适用接口 | 成功 | 业务失败 |
| --- | --- | --- | --- |
| **A. 原始 JSON** | 工作台五件套（commands / state / model-settings / cancel）、`/agent-runs/<id>/events`、`/agent-protocol/*` | HTTP 200 + 原始对象 | 工作台：**HTTP 200** + `{type:"error",...}`；`/agent-protocol`：真实 4xx/5xx + `{detail}` |
| **B. SSE** | 所有 `/stream` | HTTP 200 + `text/event-stream` | 建流前的异常退化为平台信封（HTTP 200） |
| **C. 平台信封** | 应用配置、Skills、Workspace、Memory、测评、任务中心会话 | HTTP 200 + `{code, message, data}` | HTTP 200 + `code != success` |

平台信封（风格 C，`pkg/response/response.py`）：

```json
{"code": "success", "message": "", "data": {...}}
```

`code` 取值：`success` / `fail` / `not_found` / `unauthorized` / `forbidden` / `validate_error`。
**业务异常统一走全局异常处理器，HTTP 状态码固定 200**（仅限流 429 例外）。因此**不要用 HTTP 状态码判断风格 C 接口的成败**。

未归类的异常（非项目自定义异常）在 `debug` 或 `FLASK_ENV=development` 时**原样抛出**（便于本地排障），否则返回：

```json
{"code": "fail", "message": "服务器内部错误，请稍后重试", "data": {}}
```

数据库连接类异常固定返回 `{"code":"fail","message":"数据库连接异常，请稍后重试","data":{}}`。

`validate_error_json` 会把第一条字段错误提到 `message`，并把全部字段错误放进 `data`：

```json
{"code": "validate_error", "message": "path 不能为空", "data": {"path": ["path 不能为空"]}}
```

### 1.2 状态码速查

| 状态码 | 出现位置 |
| --- | --- |
| `200` | 几乎全部接口的成功响应，**以及所有业务失败**（除限流） |
| `202` | `POST /agent-protocol/threads/<id>/runs` 创建成功 |
| `204` | `POST /agent-protocol/threads/<id>/runs/<rid>/cancel` 成功（空体） |
| `302` | Workspace 文件下载（跳转 COS 签名 URL） |
| `401` | `/agent-protocol/*` 凭证缺失/无效/过期 |
| `403` | `/agent-protocol/*` 越权（账号/应用范围不符） |
| `404` | `/agent-protocol/*` 会话或 Run 不存在 |
| `409` | `POST /agent-protocol/threads/<id>/runs` 被 `RunRejected` |
| `422` | `/agent-protocol/*` 请求体格式错误 |
| `429` | 触发限流（带 `Retry-After`） |
| `500` | `/agent-protocol/*` 未归类的业务异常 |

### 1.3 鉴权

| Blueprint | 凭证 | 解析结果 |
| --- | --- | --- |
| `my_dify` | `Authorization: Bearer <JWT access_token>` | `Account`（`JwtService.parse_token` + `payload["sub"]`） |
| `openapi` | `Authorization: Bearer <API Key>` | `Account`（`ApiKeyService.get_api_by_by_credential` + `is_active`） |
| 其余 | 不认证 | `None` |

- `my_dify` 上的绝大多数业务接口都带 `@login_required`，未登录会被拒。
- **`/agent-protocol/*` 例外**：路由注册在 `my_dify` 蓝图，但**没有 `@login_required`**，flask_login 不参与。唯一凭证是内部 HMAC Token。
- WebApp 路径里的 `<token>` **不是凭证**，仅用于定位应用，仍需登录态（`default` 角色）。
- 调用方身份（`account_id` / `created_by` / `end_user_id` / `invoke_from`）全部由服务端从登录态或凭证推导，**命令体与 body 无法覆盖**。

**API Key 格式**：`llmops-v1/` + `secrets.token_urlsafe(48)`。

### 1.4 分页

两种分页均定义在 `pkg/paginator`，但**页码分页的响应体有两种形态**，不要混用：

| 类型 | 参数 | 约束 |
| --- | --- | --- |
| 页码分页 `PaginatorReq` | `current_page`、`page_size` | 1–9999、1–50（均可不传，默认 1 / 20） |
| 游标分页 `CursorPaginatorReq` | `before`（UUID 游标）、`page_size` | 1–50 |

响应形态：

| 形态 | 使用者 | 结构 |
| --- | --- | --- |
| `PageModel` | `GET /apps`、`GET /apps/<id>/publish-histories` | `{"list": [...], "paginator": {"total_page", "total_record", "current_page", "page_size"}}` |
| 测评内联分页 | `GET /apps/<id>/evaluation-sources`、`GET /apps/<id>/evaluations` | `{"list": [...], "total", "current_page", "page_size"}`（**无 `paginator` 包装**） |
| 游标分页 | `GET /tasks/conversations` | `{"list": [...], "page": {"has_more", "next_before"}}` |
| Run 事件 | `GET /agent-runs/<id>/events` | `{"run_id", "events", "next_page"}` |

---

## 2. 公共数据结构

### 2.1 工作台命令 `WorkbenchCommandReq`

`POST .../commands` 的请求体（`schema/agent/agent_workbench_schema.py:163-215`）：

```json
{
  "type": 0,
  "id": 1,
  "method": "run.start",
  "params": { }
}
```

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| `type` | any | 透传，不参与校验 |
| `id` | int | **严格整数**（`StrictInt`），不接受 `"1"` |
| `method` | string | 枚举：`run.start` / `input.respond` / `input.reopen` |
| `params` | object | 按 `method` 解析为不同结构 |

解析失败统一抛 `ValidateErrorException`：
- 非对象：`工作台命令必须是 JSON 对象`
- 结构错误：`工作台命令格式错误: {pydantic 详情}`
- 未知方法：`不支持的工作台命令`

#### 2.1.1 `run.start` 的 params

```json
{
  "messages": [
    {"id": "msg-1", "type": "human", "content": "帮我分析这份数据"}
  ],
  "query": "帮我分析这份数据",
  "planning_enabled": false,
  "reasoning_effort": "high",
  "model_selection": {
    "provider": "openai",
    "model": "gpt-4o",
    "provider_connection_id": "3f1c...-uuid"
  },
  "image_urls": ["https://.../a.png"],
  "file_ids": ["9a2b...-uuid"],
  "selected_skills": ["data-analysis"]
}
```

| 字段 | 约束 |
| --- | --- |
| `messages` | 数组；每项 `id` ≤255，`type`/`role` ∈ `human`/`user`，`content` 任意；允许额外字段 |
| `query` | 字符串；与 `messages` 至少提供一个 |
| `planning_enabled` | 布尔，默认 `false` |
| `reasoning_effort` | 字符串，1–64 |
| `model_selection` | `{provider(1-128), model(1-255), provider_connection_id(UUID)}`；`extra="forbid"` |
| `image_urls` | ≤10 条 |
| `file_ids` | ≤5 条（附件归属会被校验） |
| `selected_skills` | ≤20 条，每条 1–64 字符、去首尾空白、不可重复 |

**兼容写法**：旧客户端可把上述内容包在 `params.input` 里（`value.get("input", value)`），例如 `{"method":"run.start","params":{"input":{"query":"..."}}}`。两者都没有时：`run.start 需要 input.messages 或 query`；解析后 `query` 为空则 `run.start 需要非空的用户输入`。

派生行为：`input_message_id` 自动取 `messages` 最后一条的 `id`（用于幂等去重）。

#### 2.1.2 `input.respond` 的 params

```json
{
  "responses": [
    {"namespace": "", "interrupt_id": "interrupt-abc", "response": {"plan": "b"}}
  ]
}
```

| 字段 | 约束 |
| --- | --- |
| `responses` | 1–40 条 |
| `responses[].namespace` | **`max_length=0`**，必须为空字符串（只支持根 Agent 的表单） |
| `responses[].interrupt_id` | 1–255 |
| `responses[].response` | object，`{字段名: 值}` |

- 关闭 `extra`，多余字段会被拒绝。
- 重复 `interrupt_id`：`中断 ID 不能重复`。
- 兼容 SDK 的单表单写法（自动归一化为一条 `responses`）。
- **必须一次性覆盖全部待填表单**，只提交一部分会被拒绝。
- 格式错误：`表单提交格式错误`。

#### 2.1.3 `input.reopen` 的 params

```json
{"run_id": "d4e5...-uuid", "cancellation_sequence": 12}
```

| 字段 | 约束 |
| --- | --- |
| `run_id` | UUID |
| `cancellation_sequence` | 严格整数，> 0 |

格式错误：`表单恢复格式错误`。

### 2.2 命令响应

**成功**（`WorkbenchCommandSuccessResp`）：

```json
{"type": "success", "id": 1, "result": {"run_id": "..."}, "meta": {"applied_through_seq": 28}}
```

- `id`：回显命令 `id`；未提供时为 `0`。
- `meta`：命中幂等重放时给出已应用到的事件序号。

**失败**（`WorkbenchCommandErrorResp`）：

```json
{"type": "error", "id": 1, "error": "invalid_argument", "message": "当前任务已有正在执行的 Agent"}
```

- **HTTP 状态码仍是 200**。
- `error` 固定为 `invalid_argument`。
- 表单字段级错误会带 `field_errors: {interrupt_id: {字段名: 错误信息}}`。
- `meta` / `field_errors` 为 `null` 时字段被省略（`exclude_none=True`）。

### 2.3 工作台事件 `WorkbenchEvent`

SSE 与 `/agent-runs/<id>/events` 的载荷：

```json
{
  "type": "event",
  "event_id": "8f2c...:27",
  "seq": 27,
  "method": "messages",
  "params": {
    "namespace": [],
    "timestamp": 1735689600000,
    "node": null,
    "data": { },
    "metadata": {"my_dify": {"source": "agent_v2", "source_seq": 27}}
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `event_id` | `<run_id>:<seq>`，可直接用作 `Last-Event-ID` |
| `seq` | ≥0，会话+入口级单调递增；**允许跳号**（增量合并所致），必须用 `seq` 推进游标而非 `+1` |
| `method` | `messages` / `values` / `tools` / `lifecycle` / `custom` / `input.requested` |
| `params.namespace` | 字符串数组；子 Agent 事件带前缀（如 `["tools:xxx"]`） |
| `params.timestamp` | 毫秒时间戳 |
| `params.metadata.my_dify.source` | `agent_v2`（内核事件）或 `workbench`（业务生成事件） |

`params.data` 为开放对象，按 `method` 有不同结构，见 §5。

### 2.4 状态响应 `WorkbenchStateResp`

```json
{
  "values": {
    "todos": [...],
    "files": {...},
    "async_tasks": [...],
    "pending_inputs": [{"interrupt_id": "...", "payload": {...}, "namespace": []}],
    "input_history": [...],
    "input_recovery": null,
    "messages": [ ... ],
    "message_page": {"has_more": false, "next_before": null},
    "runs": [ ... ]
  },
  "next": ["agent"],
  "tasks": [],
  "metadata": {"run_id": "...", "status": "running", "last_event_sequence": 27}
}
```

- `values` 只含白名单键：`todos / files / async_tasks / pending_inputs / input_history / input_recovery`，另加状态查询补入的 `messages / message_page / runs`。
- `next`：仅 `status == "running"` 时为 `["agent"]`，其余为空数组。**【缺陷】** `waiting_input` 仍占用会话，但 `next` 为空。
- `tasks`：仅 `status == "waiting_input"` 时非空，形如
  `{"id": "<interrupt_id>", "name": "ask_user", "interrupts": [{"id": "...", "value": {...}, "ns": []}]}`。
- `metadata`：无 Run 时为 `{"run_id": null, "status": "idle", "last_event_sequence": 0}`。
- `values.messages` 为最近 **20** 条业务历史，结构见 §2.5。

`values.runs[]`（`WorkbenchRunSummaryResp`）：

```json
{"run_id": "...", "message_id": "...", "input_message_id": "msg-1", "status": "completed",
 "last_event_sequence": 27, "created_at": "...", "started_at": "...", "finished_at": "...", "error": null}
```

### 2.5 历史消息 `WorkbenchHistoryMessageResp`

```json
{
  "id": "msg-1",
  "type": "human",
  "content": "帮我分析这份数据",
  "additional_kwargs": {
    "business_message_id": "...",
    "run_id": "...",
    "image_urls": [],
    "attachments": []
  }
}
```

- `type` ∈ `human` / `ai`。
- human：`id` = Run 的 `input_message_id`，缺失时为 `<message_id>:human`。
- ai：`id` = `<message_id>:assistant`，`additional_kwargs` 为 `{run_id, metrics:{latency, total_token_count, input_token_count, output_token_count, cached_token_count, reasoning_token_count}}`；**仅当回答非空时存在**。

### 2.6 模型设置 `WorkbenchModelSettingsResp`

```json
{
  "model": "gpt-4o",
  "provider": "openai",
  "selected_model": {"provider": "openai", "model": "gpt-4o", "provider_connection_id": "..."},
  "model_override": null,
  "available_models": [
    {"provider": "openai", "model": "gpt-4o", "provider_connection_id": "...",
     "label": "GPT-4o", "connection_name": "默认连接",
     "reasoning_levels": ["minimal","low","medium","high"], "default_reasoning_effort": "medium"}
  ],
  "reasoning_levels": ["minimal","low","medium","high"],
  "default_reasoning_effort": "medium",
  "selected_reasoning_effort": "high"
}
```

- 只有 `debugger` / `creator_task` / `assistant_agent` 入口会返回非空 `available_models`；`web_app` 与 `service_api` 不提供切换能力。
- `available_models` 只暴露标识与展示名，**不含凭证、地址或任意参数**。

### 2.7 取消响应 `WorkbenchCancelResp`

```json
{"cancel_requested": true, "run_id": "...", "status": "running"}
```

- `status` 是**取消请求后的真实状态**：等待表单的 Run 会立即变为 `cancelled`；执行中的 Run 仍是 `running`（取消不是状态位，见业务文档 §4.4）。
- Run 已是终态时 `cancel_requested` 为 `false`。

---

## 3. 工作台接口（五个入口共用一套）

每个入口都有相同的五个端点，路径前缀不同、固定 `invoke_from` 不同：

| 入口 | 路径前缀 | `invoke_from` | 会话授权 |
| --- | --- | --- | --- |
| 调试页 | `/apps/<uuid:app_id>/conversations/<uuid:conversation_id>` | `debugger` | `AppService.get_debugger_conversation` |
| WebApp | `/web-apps/<string:token>/conversations/<uuid:conversation_id>` | `web_app` | `WebAppService.get_owned_web_conversation` |
| 辅助 Agent | `/assistant-agent/conversations/<uuid:conversation_id>` | `assistant_agent` | `AssistantAgentService.get_owned_conversation` |
| Creator 任务 | `/tasks/apps/<uuid:app_id>/conversations/<uuid:conversation_id>` | `creator_task` | `CreatorTaskService.get_owned_conversation` |
| 开放 API | `/openapi/apps/<uuid:app_id>/conversations/<uuid:conversation_id>` | `service_api` | `ConversationService.get_service_api_conversation` |

五个入口的端点后缀完全一致：

| 方法 | 后缀 | 说明 |
| --- | --- | --- |
| `POST` | `/commands` | 提交命令 |
| `POST` | `/stream` | 订阅 SSE 事件流 |
| `GET` | `/state` | 读取状态快照 |
| `GET` | `/model-settings` | 读取模型设置 |
| `POST` | `/runs/<uuid:run_id>/cancel` | 取消指定 Run |

各入口传给服务层的身份参数（客户端无法覆盖）：

| 入口 | `account_id` | `created_by` | `end_user_id` |
| --- | --- | --- | --- |
| 调试页 | `current_user.id` | `current_user.id` | 不传 |
| WebApp | `current_user.id` | `end_user.id` | `end_user.id` |
| 辅助 Agent | `current_user.id` | `current_user.id` | 不传 |
| Creator 任务 | `current_user.id` | `current_user.id` | 不传 |
| 开放 API | `current_user.id`（API Key 账号） | `conversation.created_by`（EndUser） | `conversation.created_by` |

服务层把 `end_user_id or created_by` 记为 `AgentRunContext.principal_id`，但该字段目前**未被任何逻辑读取**。长期记忆与 Workspace 的隔离身份只看 `(account_id, app_id)`：

- **Workspace**：`uuid5(NAMESPACE_URL, "my-dify:agent-workspace:v1:{account}:{app}:{account}")` → 按账号 + 应用共享，跨会话、跨终端用户共享。
- **长期记忆**：Store 命名空间 `("mydify", "agent-memory", <account_id>, <app_id>)` → 同样按账号 + 应用共享。

含义：同一个 WebApp / 开放 API 应用下的**所有终端用户共享同一份记忆和同一份 Workspace 文件**，终端用户之间没有隔离。

---

### 3.1 `POST .../commands`

| 项 | 值 |
| --- | --- |
| 鉴权 | 登录态（辅助 Agent 创建会话除外） |
| 限流 | 无 |
| 请求 | `WorkbenchCommandReq`（§2.1） |
| 响应 | `WorkbenchCommandSuccessResp` / `WorkbenchCommandErrorResp`（§2.2），**HTTP 200** |

`run.start` 会把 `user_input_enabled` 默认置为 `true`：**全部五个工作台入口的 Handler 都没有传这个参数**（`internal/handler` 下没有任何 `user_input_enabled` 引用）。只有内部 SDK 的 `create_run` 与 `replace_run` 显式传 `false`。最终是否暴露 `ask_user` 还要求应用配置 `user_input.enable = true`。

**命令失败提示语**（`message` 字段）：

| 场景 | 文案 |
| --- | --- |
| 同一会话已有运行中任务 | `当前任务已有正在执行的 Agent` |
| 同一会话停在表单 | `请先完成当前表单` |
| 前序任务正在停止 | `当前任务正在停止，请稍后重试` |
| 表单被取消（本入口） | `此任务已在表单处停止，请点击重新填写后继续，或创建新会话` |
| 表单被取消（其他入口） | `此任务已在表单处停止，请创建新会话` |
| 输入消息被其他入口占用 | `输入消息已用于其他调用入口` |
| 答案保存竞态 | `任务正在保存，请稍后重试` |
| 提交时配置已变更 | `应用配置已变更，请恢复原配置后继续填写，或创建新会话` |
| 表单重复修改 | `该表单已提交，不能重复修改` |
| 恢复非最新表单 | `仅支持恢复当前会话最新停止的表单，请刷新后重试` |
| `replace_run` 等待超时 | `前序任务尚未停止，本次更新未启动，请稍后重试` |
| 执行槽已满 | **不是命令错误**，`/commands` 直接抛平台信封异常；文案 `当前 Agent 执行繁忙，请稍后重试` |

**幂等语义**：

| 命令 | 重复提交结果 |
| --- | --- |
| `run.start`（同 `input_message_id`、同入口） | 返回**同一个** `run_id`，`meta.applied_through_seq` = 该 Run 最小事件序号 − 1（从头重放） |
| `input.respond`（同批次答案） | 返回首次接受的 `run_id` / `meta.applied_through_seq` |
| `input.reopen`（同取消批次） | 返回首次恢复的 `run_id` / `meta.applied_through_seq` |

批次的 `input_batch_id` = 中断 ID 集合排序后的 SHA-256，**与答案值和键顺序无关**；同批次换答案会被拒。

---

### 3.2 `POST .../stream`

| 项 | 值 |
| --- | --- |
| 响应 | `text/event-stream` |
| 请求体 | `WorkbenchEventStreamReq` |
| 断点续传 | 请求头 `Last-Event-ID`，或请求体 `since`（取两者较大值） |

请求体：

```json
{
  "run_id": "8f2c...-uuid",
  "since": 0,
  "channels": ["messages", "values", "tools", "lifecycle", "custom", "input"],
  "namespaces": [["tools:abc"]],
  "depth": 3
}
```

| 字段 | 约束 |
| --- | --- |
| `run_id` | **必填**，UUID |
| `since` | ≥0，默认 0 |
| `channels` | 枚举：`messages` / `values` / `tools` / `lifecycle` / `input` / `checkpoints` / `custom`；空数组表示不过滤。`input` 映射到 `input.requested`；`checkpoints` 仅为兼容 LangGraph SDK 而接受，**当前不发布** |
| `namespaces` | 前缀匹配；空数组表示不过滤 |
| `depth` | ≥0；仅保留 `namespace` 长度 ≤ 该值的事件 |

**`Last-Event-ID` 解析规则**（`_parse_last_event_sequence`）：
- 普通形式 `<run_id>:<seq>` → `seq`；
- `<run_id>:cursor-reset:<n>` → `n - 1`（重放起点减一）；
- 解析失败 → 忽略该头；
- 解析成功后 `since = max(请求体 since, 解析值)`。

参数非法：`工作台订阅参数错误: {pydantic 详情}`。

**响应格式**：

```
: connected

id: 8f2c...:1
event: lifecycle
data: {"type":"event","event_id":"8f2c...:1",...}

: heartbeat

id: 8f2c...:2
event: messages
data: {...}
```

- 首行固定 `: connected`；每 15 秒空闲发一次 `: heartbeat`（**不是**业务事件）。
- 事件行固定顺序 `id:` → `event:`（= `method`）→ `data:`，`data` 为单行紧凑 JSON（`ensure_ascii=False`）。
- 响应头：`Cache-Control: no-cache, no-transform`、`X-Accel-Buffering: no`。
- **流会在根 Run 进入终态且事件读尽时自行结束**；`waiting_input` 不是终态，流会保持打开直到下一次终态。
- 订阅前会先按 `run_id` 做归属校验（`NotFoundException("Agent Run 不存在或不属于当前会话")`），并顺带收敛孤儿 Run，保证重连能读到真实终态。

---

### 3.3 `GET .../state`

无请求参数。响应为 `WorkbenchStateResp`（§2.4）。
会话权限校验由各入口 Handler 完成；查询服务内部还会先收敛孤儿 Run。

---

### 3.4 `GET .../model-settings`

无请求参数。响应为 `WorkbenchModelSettingsResp`（§2.6）。

- `selected_reasoning_effort` 由该会话该入口**最新 Run** 的调用快照恢复。
- `available_models` 的可见范围：模型连接属于当前账号且创建者为账号本人。

---

### 3.5 `POST .../runs/<uuid:run_id>/cancel`

无请求体。响应为 `WorkbenchCancelResp`（§2.7）。

- Run 必须同时属于本次请求的**应用、会话、入口**，否则 `Agent Run 不存在或不属于当前会话`。
- 幂等：重复调用不会刷新 `cancel_requested_at`，终态 Run 返回 `cancel_requested: false`。

---

### 3.6 各入口的额外端点

#### 3.6.1 辅助 Agent：`POST /assistant-agent/conversations`

| 项 | 值 |
| --- | --- |
| 限流 | `user` —— **40 请求 / 60 秒**（滑动窗口，装饰器在 `@login_required` 外层，未认证也计入） |
| 请求体 | 无 |
| 响应 | `{"conversation_id": "..."}` |

创建或**复用**当前账号唯一的辅助 Agent 会话（同一账号永远只有一个）。`app_id` 固定为 `config.ASSISTANT_AGENT_ID`，不校验 App 记录是否存在。

#### 3.6.2 开放 API：`POST /openapi/apps/<uuid:app_id>/conversations`

| 项 | 值 |
| --- | --- |
| 鉴权 | API Key（`openapi` 蓝图） |
| 限流 | 无 |
| 请求体 | 可选 `{"end_user_id": "<uuid>"}` |
| 响应 | `{"conversation_id": "..."}` |

- `end_user_id` 非合法 UUID：`end_user_id 必须是有效 UUID`（平台信封）。
- 应用必须 `published` 且属于当前账号，否则 `应用不存在、未发布或无权访问`。
- 传入的 `end_user_id` 必须属于该应用，否则 `终端用户不属于当前应用`；不传则由服务端新建 `EndUser`。
- 该会话的 `created_by` / `end_user_id` 是 **EndUser.id**，而 `account_id` 是 API Key 所属账号。

#### 3.6.3 Run 事件查询：`GET /agent-runs/<uuid:run_id>/events`

| 项 | 值 |
| --- | --- |
| 鉴权 | 登录态 |
| 查询参数 | `current_page`（默认 1，1–9999）、`page_size`（默认 20，1–50） |
| 响应 | `{"run_id": "...", "events": [WorkbenchEvent...], "next_page": 2 \| null}`（**原始 JSON**） |

- 分页参数非法：`执行详情分页参数无效`（平台信封）。
- 仅能读取自己名下会话的 Run（内部按会话归属校验），Run 不存在时 `Agent Run 不存在`。
- `events` 已应用与 SSE 相同的公开投影（含 `custom` 白名单过滤），因此**可能有被跳过的内部事件**；`next_page` 依据数据库行数判断，与过滤后的条数不一定对应。

---

## 4. 内部 Agent Protocol（LangGraph SDK 兼容）

供 DeepAgents `AsyncSubAgent`（已发布异步子 Agent）调用。前缀 `/agent-protocol`，可通过 `AGENT_INTERNAL_PROTOCOL_BASE_URL` 配置（默认 `http://127.0.0.1:5000/agent-protocol`）。

**这些路由挂载在 `my_dify` 蓝图，但没有 `@login_required`。** 唯一凭证是内部 HMAC Token：

| 头 | 说明 |
| --- | --- |
| `X-MyDify-Internal-Token: <token>` | 优先读取，读取时 `strip()` |
| `Authorization: Bearer <token>` | 兼容 SDK；仅在无自定义头时使用，且要求恰好两段且首段为 `bearer`（不区分大小写） |

Token 结构：`<base64url(payload)>.<base64url(HMAC-SHA256)>`，payload `{v:1, account_id, parent_app_id, target_app_id, exp}`。
密钥：`AGENT_INTERNAL_PROTOCOL_SECRET`（空时回退 `JWT_SECRET_KEY`）；TTL：`AGENT_INTERNAL_PROTOCOL_TOKEN_TTL_SECONDS`（默认 86400 秒，下限 60）。
签发会**剔除调用方传入的任何大小写变体的 `X-MyDify-Internal-Token`**，防止伪造。

**授权范围（每次调用都复核）**：账号存在 → 父应用属于该账号 → 目标应用属于该账号**且已发布**。否则 `403 内部 Agent Protocol 调用不在有效应用范围内`。

**错误响应**：所有异常都返回 `{"detail": "<message>"}`：

| 状态 | 触发 |
| --- | --- |
| 401 | 凭证缺失/无效/过期（`内部 Agent Protocol 凭证无效` / `…凭证已过期`） |
| 403 | 应用范围不符、`graph_id` 不在授权范围 |
| 404 | 会话或 Run 不存在/无权访问 |
| 409 | 创建 Run 被拒绝（并发占用等） |
| 422 | 请求体格式错误 |
| 500 | 其他业务异常 |

会话或 Run 不存在时返回 404 `内部 Agent Protocol 会话不存在或无权访问`（归属校验先于任何写入，孤儿收敛在授权确认之后才执行）。

---

### 4.1 `POST /agent-protocol/threads`

| 项 | 值 |
| --- | --- |
| 请求体 | 无 |
| 成功 | `200` + `InternalThreadResp` |

固定创建一条 `name = "新的会话"`、`invoke_from = creator_task` 的会话。

```json
{
  "thread_id": "...",
  "created_at": "2025-01-01T00:00:00Z",
  "updated_at": "2025-01-01T00:00:00Z",
  "metadata": {"graph_id": "mydify:published:<target_app_id>"},
  "status": "idle",
  "values": {},
  "interrupts": {}
}
```

`status` ∈ `idle` / `busy`；`values` / `interrupts` 固定为空对象。

### 4.2 `GET /agent-protocol/threads/<uuid:conversation_id>`

成功：`200` + `InternalThreadResp`。

- `values.messages` **只包含最新一条**业务消息（内部按 `page_size=1` 读取），结构：
  - human：`{"type":"human","id":"<message_id>:human","content":"<query>"}`；
  - ai：当 `answer` 非空时追加 `{"type":"ai","id":"<message_id>:assistant","content":"<answer>"}`。
- `status`：最新 Run 状态 ∈ `(running,)` 时 `busy`，否则 `idle`。
  **【缺陷】** 停在表单的 Run（`waiting_input`）在这里报 `idle`，而 `GET .../runs/<id>` 对同一个 Run 报 `running`。
- `interrupts` **恒为空对象**，SDK 客户端无法从线程端点发现待填表单。

### 4.3 `POST /agent-protocol/threads/<uuid:conversation_id>/runs`

| 项 | 值 |
| --- | --- |
| 请求体 | `InternalRunCreateReq` |
| 成功 | **`202`** + `InternalRunResp` |
| 被拒 | **`409`** + `{"detail": "<RunRejected.message>"}` |

请求体（`extra` 被忽略）：

```json
{
  "assistant_id": "mydify:published:<target_app_id>",
  "input": {"query": "…", "messages": [...]},
  "multitask_strategy": "reject"
}
```

| 字段 | 约束 |
| --- | --- |
| `assistant_id` | 必填；必须**精确等于** `mydify:published:<target_app_id>`，否则 `内部 Agent Protocol graph_id 不在授权范围内` |
| `input` | 与 `command` **必须且只能提供一个**，否则 `Run 必须且只能提供 input 或 command` |
| `command` | 取 `command["update"]` 作为运行输入 |
| `multitask_strategy` | `reject`（默认）/ `interrupt` |

行为：

1. `input` / `command` 都缺失：`后台 Run 缺少 input`。
2. 输入经 `WorkbenchCommandInputReq` 校验，失败：`后台 Run 输入格式错误: {exc}`。
3. `multitask_strategy == "interrupt"` → `replace_run`（取消前序、等待 ≤10 秒、再启动）；否则 `start_run`。
4. **始终以 `user_input_enabled = false` 启动**：SDK 入口不会暴露 HITL 表单。
5. 并发占用等业务拒绝以 `409 {"detail": ...}` 返回。

响应：

```json
{
  "run_id": "...",
  "thread_id": "...",
  "assistant_id": "mydify:published:<app_id>",
  "status": "running",
  "multitask_strategy": "reject",
  "metadata": {},
  "created_at": "2025-01-01T00:00:00Z",
  "updated_at": "2025-01-01T00:00:00Z",
  "error": null
}
```

`status` 枚举 `pending` / `running` / `success` / `error` / `cancelled`。状态映射：

| 内部 Workbench 状态 | SDK `status` |
| --- | --- |
| `running` | `running` |
| `waiting_input` | `running` |
| `completed` | `success` |
| `failed` | `error` |
| `cancelled` | `cancelled` |
| 其他 | `error` |

`error` = `row.error_message`，为空且映射为 `error` 时取 `Agent 运行失败`。

### 4.4 `GET /agent-protocol/threads/<uuid:conversation_id>/runs/<uuid:run_id>`

成功：`200` + `InternalRunResp`（同 §4.3）。会话/Run 不属于该账号：`404 内部 Agent Protocol 会话不存在或无权访问`。

### 4.5 `POST /agent-protocol/threads/<uuid:conversation_id>/runs/<uuid:run_id>/cancel`

成功：**`204`** + 空体。取消请求被丢弃（已是终态等）也返回 204。

### 4.6 缺失的能力

- **没有 `PATCH` / `update_run` 路由**。"更新"只能通过 `multitask_strategy: "interrupt"` 或 `command.update` 表达。
- **没有流式端点**（`/runs/stream`、`/threads/<id>/stream`）。SDK 侧如需事件，需回到工作台 `/stream`。

---

## 5. 事件数据（`params.data`）

### 5.1 `lifecycle`

| `data.event` | 说明 |
| --- | --- |
| `running` | `phase` ∈ `preparing` / `executing`；另含 `message_id`、`input_message_id` |
| `completed` | 正常结束（根终态由业务事务生成，内核的根终态被丢弃） |
| `failed` | 含 `error`（公开文案）与可选 `code` |
| `interrupted` | `reason` = `user_input_required`（等待表单）或 `cancel_requested`（已取消） |

子 Agent 的 lifecycle **不会**被丢弃，带 `namespace` 前缀下发。

### 5.2 `messages`

| `data.event` | 说明 |
| --- | --- |
| `message-start` | 根 Agent 的 ai 消息 ID 被重绑为 `<message_id>:assistant` |
| `content-block-start` | `delta.type` ∈ `text` / `reasoning` |
| `content-block-delta` | `delta` 为 `{type: "text-delta", text}` 或 `{type:"reasoning-delta", reasoning}`；相邻同类增量可能被**合并**，因此 `seq` 会跳号 |
| `content-block-finish` | — |
| `message-finish` | — |

工具参数与签名不下发；摘要来源（`lc_source == "summarization"`）的消息被丢弃。

### 5.3 `tools`

`data.event` ∈ `tool-started` / `tool-output-delta` / `tool-finished` / `tool-error`，以 `tool_call_id` 关联。
`tool-started` 的工具名兼容读取 `tool_name` / `name`。

### 5.4 `values`

只含公开键（`todos / files / async_tasks / pending_inputs / input_history / input_recovery`）。
`async_tasks[*]` 的框架字段 `thread_id` 被改名为 `conversation_id`。

### 5.5 `custom`（白名单）

**只有两种结构会被下发**，其余自定义数据被静默丢弃：

```json
{"name": "context_window", "payload": {"type": "context_window", "..." : "..."}}
```

`payload.type` ∈ `context_window` / `context_compaction`，且必须通过对应 Pydantic 校验。

### 5.6 `input.requested`

等待用户输入时按待填表单逐条发出：

```json
{"interrupt_id": "interrupt-abc", "payload": {"title": "...", "fields": [...]}}
```

`method` 为 `input.requested`，但在 `channels` 过滤中对应 `"input"`。

---

## 6. Creator 任务中心

前缀 `/tasks`。全部要求登录态，响应为**平台信封**。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/tasks/agents` | 当前账号可运行（已发布）的 Agent 列表 |
| `GET` | `/tasks/agents/<uuid:app_id>` | 单个已发布 Agent 的运行配置 |
| `GET` | `/tasks/conversations` | 跨入口会话历史（游标分页） |
| `POST` | `/tasks/apps/<uuid:app_id>/conversations` | 创建任务会话 |
| `GET` | `/tasks/conversations/<uuid:conversation_id>` | 单个会话详情 |
| `PATCH` | `/tasks/conversations/<uuid:conversation_id>/name` | 重命名 |
| `PATCH` | `/tasks/conversations/<uuid:conversation_id>/is-pinned` | 置顶/取消置顶 |
| `DELETE` | `/tasks/conversations/<uuid:conversation_id>` | 删除（软删 + 资源清理） |

### 6.1 `GET /tasks/agents`

响应 `data = {"list": [...]}`，每项：

```json
{"id": "...", "name": "...", "icon": "...", "description": "...",
 "status": "published", "conversation_count": 3, "last_active_at": 1735689600}
```

### 6.2 `GET /tasks/agents/<uuid:app_id>`

响应 `data`：

```json
{"id": "...", "name": "...", "icon": "...", "description": "...", "app_config": {...}}
```

- 只返回**已发布**配置的运行时投影；**不返回 WebApp token**。
- 不存在/未发布/非本人：`应用不存在、未发布或无权访问`。

### 6.3 `GET /tasks/conversations`

| 查询参数 | 约束 |
| --- | --- |
| `before` | 游标 UUID；非法时 `before必须是合法的UUID` |
| `page_size` | 1–50，默认 20 |
| `app_id` | 可选 UUID；非法时 `app_id必须是合法的UUID` |
| `search` | 可选，≤255 字符；匹配会话 `name` 与 `summary`（模糊） |

响应 `data`：

```json
{
  "list": [{
    "id": "...", "name": "...", "summary": "...", "is_pinned": false,
    "updated_at": 1735689600, "created_at": 1735689600, "last_run_status": "completed",
    "agent": {"id": "...", "name": "...", "icon": "...", "description": "..."}
  }],
  "page": {"has_more": true, "next_before": "..."}
}
```

- 排序：`updated_at DESC, id DESC`；`next_before` 为本页最后一条的会话 ID。
- 时间戳为 **Unix 秒**（整数）。
- 可见范围：`App.account_id = 当前账号` **且** `App.status = published` **且** `Conversation.invoke_from ∈ (creator_task, web_app)` **且** 未删除，并满足所有权规则（`owner_account_id` 为本人，或为空时 `created_by` 为本人 / WebApp 场景下为本人名下 EndUser）。

### 6.4 `POST /tasks/apps/<uuid:app_id>/conversations`

无请求体。创建 `name = "新的会话"`、`invoke_from = creator_task` 的会话，响应 `data` 为单条会话摘要（结构同 §6.3 的列表项）。

### 6.5 `GET /tasks/conversations/<uuid:conversation_id>`

响应 `data` 为单条会话摘要。不可见：`会话不存在或无权访问`。

### 6.6 `PATCH /tasks/conversations/<uuid:conversation_id>/name`

| 项 | 值 |
| --- | --- |
| 请求体 | JSON：`{"name": "..."}`（**不是**表单） |
| 校验 | 必须是字符串（`会话名称必须是字符串`）、非空（`会话名称不能为空`）、≤255（`会话名称不能超过255个字符`） |

响应 `data` 为更新后的会话摘要（`name` 会做 `strip()`）。

### 6.7 `PATCH /tasks/conversations/<uuid:conversation_id>/is-pinned`

| 项 | 值 |
| --- | --- |
| 请求体 | JSON：`{"is_pinned": true}` |
| 校验 | 必须**显式提供**且为布尔值，否则 `is_pinned必须是布尔值`（字符串 `"true"` 不通过） |

响应 `data` 为更新后的会话摘要。

### 6.8 `DELETE /tasks/conversations/<uuid:conversation_id>`

先清理会话级资源（沙箱、checkpoint、消息级记忆），再软删除会话。成功：`{"code":"success","message":"删除会话成功","data":{}}`。

**注意**：清理**不会**删除 Workspace 文件（Workspace 按「账号 + 应用」共享，跨会话保留）。

---

## 7. 应用配置（Agent 定义）

平台信封 + 登录态。以下路由与 Agent 定义直接相关（`internal/router/app_routes.py`）。

### 7.1 `GET /apps` / `POST /apps`

| 方法 | 路径 | 请求 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/apps` | 查询参数（页码分页 + `search_word` + `status`） | 应用分页列表 |
| `POST` | `/apps` | **表单字段**（`CreateAppReq`，非 JSON） | 创建应用 |

> `GET /apps` 的响应使用 `PageModel` 形态：`data = {"list": [...], "paginator": {...}}`。

`GET /apps` 查询参数：

| 参数 | 约束 |
| --- | --- |
| `current_page` / `page_size` | 默认 1 / 20 |
| `search_word` | 可选 |
| `status` | 可选；只接受 `draft` / `published`，否则 `应用状态仅支持 draft 或 published` |

响应 `data.list[]`（`GetAppsWithPageResp`）：

```json
{
  "id": "...", "name": "...", "icon": "...", "description": "...",
  "preset_prompt": "...",
  "model_config": {"provider": "openai", "model": "gpt-4o", "provider_connection_id": "..."},
  "status": "published",
  "updated_at": 1735689600, "created_at": 1735689600
}
```

- `preset_prompt` 与 `model_config` 取**已发布**配置（`status = published`）或**草稿**配置。
- 时间均为 **Unix 秒**整数。
- `model_config` 只投影三个标识字段，不含 `parameters` 等运行参数。

`POST /apps` 请求字段（`multipart/form-data` 或 `x-www-form-urlencoded`）：

| 字段 | 校验 |
| --- | --- |
| `name` | 必填，≤40 |
| `icon` | 可选，URL 格式 |
| `description` | 可选，≤800 |

成功响应 `data = {"id": "<新应用 id>"}`。

### 7.2 `GET /apps/<uuid:app_id>` / `POST /apps/<uuid:app_id>`

| 方法 | 请求 | 说明 |
| --- | --- | --- |
| `GET` | 无 | 应用详情 |
| `POST` | **表单字段**（`UpdateAppReq`） | 更新基础信息 |

`GET` 响应 `data`：`{id, conversation_id, name, icon, description, status, draft_updated_at, updated_at, created_at}`（时间均为 **Unix 秒**整数）。

- `conversation_id` 是**该应用的调试会话** ID，由 `get_app_with_debug_conversation` 一并返回；调试工作台入口需要它。
- `POST` 成功提示：`修改Agent智能体应用成功`。

### 7.3 配置读取与更新

| 方法 | 路径 | 限流 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/apps/<uuid:app_id>/draft-app-config` | `lightweight`（60/60s） | 读取草稿配置（运行态投影） |
| `POST` | `/apps/<uuid:app_id>/draft-app-config` | `user`（40/60s） | 增量更新草稿配置 |
| `GET` | `/apps/<uuid:app_id>/published-subagents` | `lightweight` | 可引用的同账号已发布 Agent 候选 |
| `POST` | `/apps/<uuid:app_id>/publish` | `sensitive`（3/60s） | 发布 / 更新发布配置 |
| `POST` | `/apps/<uuid:app_id>/cancel-publish` | `sensitive` | 取消发布 |
| `GET` | `/apps/<uuid:app_id>/publish-histories` | `database_intensive`（25/60s） | 发布历史（页码分页） |
| `POST` | `/apps/<uuid:app_id>/fallback-history` | `user` | 回退历史版本到草稿 |
| `GET` | `/apps/<uuid:app_id>/published-config` | `lightweight` | 发布配置信息 |
| `POST` | `/apps/<uuid:app_id>/published-config/regenerate-web-app-token` | `sensitive` | 重新生成 WebApp token |

#### `GET /apps/<uuid:app_id>/draft-app-config`

响应 `data`（`_process_and_transformer_app_config` 组装）：

```json
{
  "id": "<app_config_version.id>",
  "model_config": {"provider": "...", "model": "...", "parameters": {...},
                   "provider_connection_id": "...", "features": null,
                   "modalities": null, "input_modalities": null,
                   "output_modalities": null, "native_file_input": null},
  "preset_prompt": "...",
  "skills": [{"name": "...", "version": "...", "digest": "...", "enabled": true}],
  "subagents": [ ... ],
  "tools": [{"provider_id": "...", "tool_name": "...", "params": {...}, "label": "...", "description": "..."}],
  "workflows": [{"workflow_id": "...", "name": "...", "icon": "...", "description": "..."}],
  "datasets": [{"dataset_id": "...", "name": "...", "icon": "...", "description": "..."}],
  "retrieval_config": {"search_method": "semantic", "top_k": 10, "score_threshold": 0.5},
  "long_term_memory": {"enable": false},
  "opening_statement": "...",
  "opening_questions": [],
  "user_input": {"enable": true},
  "speech_to_text": {"enable": false},
  "text_to_speech": {"enable": false, "voice": "", "language": ""},
  "suggested_after_answer": {"enable": true},
  "review_config": {"enable": false, "keywords": [],
                    "inputs_config": {"enable": false, "preset_response": ""},
                    "outputs_config": {"enable": false}},
  "updated_at": 1735689600,
  "created_at": 1735689600
}
```

- `id` 是**草稿配置版本 ID**（`app_config_version.id`），不是应用 ID。
- `tools` / `workflows` / `datasets` 已经过服务端补全（补名称、图标、描述），不是原始绑定。
- `skills` 与 `subagents` 按存储原样返回（`skills` 缺列时回退空数组）。
- `updated_at` / `created_at` 为 Unix 秒。
- 路由会传入 `request_base_url`，用于补全工具/工作流的相对图标地址。

#### `POST /apps/<uuid:app_id>/draft-app-config`

请求体为 **JSON**（`DraftAppConfigPatchReq`，`extra="forbid"`）：

```json
{
  "model_config": {...},
  "preset_prompt": "...",
  "tools": [{"provider_id": "...", "tool_name": "...", "params": {}}],
  "workflows": [...],
  "datasets": [...],
  "retrieval_config": {"search_method": "semantic", "top_k": 10, "score_threshold": 0.5},
  "skills": [{"name": "data-analysis", "version": "1.0.0", "digest": "...", "enabled": true}],
  "subagents": [...],
  "long_term_memory": {"enable": false},
  "user_input": {"enable": true},
  "review_config": {
    "enable": false,
    "keywords": [],
    "inputs_config": {"enable": false, "preset_response": ""},
    "outputs_config": {"enable": false}
  },
  "opening_statement": "...",
  "opening_questions": [],
  "suggested_after_answer": {"enable": true}
}
```

关键校验规则：

**所有 Pydantic 校验失败都统一包成一条异常**（`DraftAppConfigPatchReq.from_mapping`）：

```
草稿配置格式错误: 1 validation error for DraftAppConfigPatchReq
...
```

因此下表中的内层文案出现在 `草稿配置格式错误:` 之后，属于 `data`/`message` 的一部分。

| 规则 | 内层文案 |
| --- | --- |
| 空对象（未提供任何字段） | `草稿配置不能为空` |
| 显式传 `null` | `配置字段不能为 null: <逗号分隔字段名>` |
| 未知字段 | `extra="forbid"` 校验失败 |
| `tools` / `workflows` / `datasets` 重复或超 5 条 | `validate_unique_runtime_bindings` 报错 |
| `review_config` 只传部分键 | 四个键（`enable` / `keywords` / `inputs_config` / `outputs_config`）**必须齐全** |
| `long_term_memory` / `user_input` / `speech_to_text` / `suggested_after_answer` | 只有 `enable`（布尔，必填） |
| `preset_prompt` / `opening_statement` | ≤2000 |
| `opening_questions` | ≤3 条 |
| `skills[].name` | 正则 `^[a-z][a-z0-9-]*$`，1–64，同应用内不重复 |
| Skill 不存在或未内置 | `Skill 不存在或未内置: {name}` |
| Skill 版本与目录不符 | `Skill 版本不匹配: {name}` |
| 发布引用自引用 | `发布 Agent 不能引用当前应用自身` |
| 发布引用成环 | `发布 Agent 子 Agent 引用存在循环` |
| 发布引用嵌套过深 | `发布 Agent 子 Agent 嵌套层级不能超过 32` |

**特例**：`model_config` 传空对象 `{}` 会被规范化为"未提供"（新建 Agent 尚未选模型），**不会**报错。

**注意**：`skills[].version` 与 `digest` 会被服务端按目录**覆盖**，客户端传值不生效。

成功：`{"code":"success","message":"更新应用草稿配置成功","data":{}}`。

#### `SubAgent`（`subagents[]`）规范化后的存储结构

**没有独立的响应 Schema**；规范形态由 `app_config_service._normalize_subagent_configs` 在写入时决定，读接口按存储原样返回。**键是条件性的**，不是固定集合：

所有模式共用：

```json
{
  "name": "researcher",
  "description": "...",
  "mode": "sync",
  "context_passing": "NONE",
  "tools": ["tool_a"],
  "skills": ["data-analysis"]
}
```

按 `mode` / `source` 追加：

| 条件 | 追加的键 |
| --- | --- |
| `mode = "async"` | `source`；`graph_id`；`headers`（对象）；`source = "published"` 时追加 `published_app_id`；配置了 `url` 时追加 `url` |
| `mode = "sync"` | `system_prompt` |
| `mode = "sync"` 且 `source = "custom"` | `model_mode`；选了独立模型时追加 `model`、`model_provider`，有连接时再追加 `model_connection_id` |
| `mode = "sync"` 且 `source = "published"` | `source`、`published_app_id` |

注意：
- `mode = "sync"` + `source = "custom"` 时**没有** `source` 键（缺省即按自定义处理）。
- 自定义模型字段名是 `model` / `model_provider` / `model_connection_id`，**不是** `model_config`。
- `sync` 模式的 `system_prompt` 由平台合成（发布来源）或由客户端提供（自定义来源）。

字段约束（`internal/entity/subagent_config.py`）：

| 字段 | 约束 |
| --- | --- |
| `name` | 1–64；禁止 `.`/`..`、控制字符、`/ \ : * ? " < > \|` |
| `description` | 1–500 |
| `system_prompt` | ≤8000（仅 `sync`） |
| `tools` / `skills` | 名称列表，不可重复；Skill 必须是主 Agent 已挂载且启用的 |
| `mode` | `sync` / `async`（必填） |
| `source` | `custom` / `published` |
| `context_passing` | 固定 `NONE` |
| `model_mode` | `inherit` / `custom`（仅 `sync` + `custom`） |
| `graph_id` | ≤255；发布异步必须为 `mydify:published:<published_app_id>` |
| `headers` | ≤32 项，名 ≤128，值 ≤2048（仅 `async`） |
| 重名 | 同应用内不可重名 |

同步自定义 SubAgent 的工具必须已在主 Agent 授权集合内，否则报 `subagent '<name>' 引用了未授权工具`。

#### `GET /apps/<uuid:app_id>/published-subagents`

查询参数：`search_word`（可选）。响应 `data = {"list": [...]}`，元素为同账号**已发布** Agent 候选项：

```json
{
  "id": "...", "name": "...", "icon": "...", "description": "...",
  "status": "published",
  "model": {"provider": "openai", "model": "gpt-4o"}
}
```

#### `GET /apps/<uuid:app_id>/published-config`

响应 `data`：

```json
{
  "web_app": {"token": "<WebApp token>", "status": "published"},
  "subagent": {"enabled": true, "app_id": "...", "name": "..."}
}
```

- `subagent.enabled` 等价于 `status == published`。
- 该接口在代码中标注为 `@deprecated 已由 Agent 工作台线程生命周期管理替代，暂保留兼容`。

#### `POST /apps/<uuid:app_id>/cancel-publish`

取消发布：`status` 置回 `draft`、清空 `app_config_id`、删除该应用的知识库关联。成功提示 `取消发布应用配置成功`。
未发布时：`当前应用未发布，请核实后重试`。

#### `POST /apps/<uuid:app_id>/published-config/regenerate-web-app-token`

响应 `data = {"token": "<新 token>"}`，旧 token 立即失效。
未发布时：`应用未发布，无法生成WebApp凭证标识`。

---

## 8. Skills

平台信封 + 登录态。内置 Skill 目录来自文件系统（`BUILTIN_SKILLS_ROOT`，默认 `resources/agent_skills/builtin`），**没有 ORM 表、没有上传接口**。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/skills` | 全量内置 Skill 模板目录 |
| `GET` | `/skills/<string:skill_name>` | 单个模板（含 `SKILL.md` 正文） |
| `GET` | `/apps/<uuid:app_id>/skills` | 该应用挂载的 Skill 及启用状态 |

### 8.1 `GET /skills`

响应 `data = {"items": [BuiltinSkillResp...]}`：

```json
{
  "name": "data-analysis",
  "description": "...",
  "version": "1.0.0",
  "digest": "sha256...",
  "category": "data",
  "category_name": "数据分析",
  "execution_mode": "inline",
  "requires": [],
  "allowed_tools": ["read_file"],
  "enabled": true
}
```

`category` 枚举与中文名：`office`→办公效率、`content`→内容创作、`knowledge`→知识管理、`data`→数据分析、`education`→教育学习、`agent`→Agent 能力。
`execution_mode` ∈ `inline` / `sandbox`。

### 8.2 `GET /skills/<string:skill_name>`

在上述字段基础上追加 `content`（`SKILL.md` 全文）。不存在：`Skill 模板不存在`。

### 8.3 `GET /apps/<uuid:app_id>/skills`

读取该应用**草稿配置**中的 Skill 绑定，返回带 `enabled` 标记的目录。响应结构与 §8.1 相同。

---

## 9. Workspace 文件

平台信封 + 登录态。Workspace 按「账号 + 应用」**跨会话共享**。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/apps/<uuid:app_id>/workspace/files` | 列文件 |
| `POST` | `/apps/<uuid:app_id>/workspace/files` | 写文本文件 |
| `GET` | `/apps/<uuid:app_id>/workspace/file` | 预览 |
| `DELETE` | `/apps/<uuid:app_id>/workspace/file` | 删除 |
| `GET` | `/apps/<uuid:app_id>/workspace/file/download` | 下载（302） |
| `GET` | `/web-apps/<string:token>/workspace/files` | WebApp 侧搜索 |
| `GET` | `/web-apps/<string:token>/workspace/file` | WebApp 侧预览 |
| `GET` | `/web-apps/<string:token>/workspace/file/download` | WebApp 侧下载（302） |
| `GET` | `/web-apps/<string:token>/conversations/<uuid:conversation_id>/file` | 兼容：按会话预览 |
| `GET` | `/web-apps/<string:token>/conversations/<uuid:conversation_id>/file/download` | 兼容：按会话下载 |

### 9.1 请求参数

| 接口 | 参数位置 | 字段 | 约束 |
| --- | --- | --- | --- |
| 列表 | query | `path_prefix` | 可选，≤512 |
| 搜索（WebApp） | query | `query` | 可选，≤200 |
| 预览 / 下载 / 删除 | query | `path` | 必填，≤512（`path 不能为空`） |
| 写入 | **JSON body** | `path` | 必填，≤512 |
| | | `content` | **必须存在且为字符串**，允许空串（`content 必须是字符串`） |
| | | `mime_type` | 可选，≤128，默认 `text/plain` |

响应（`WorkspaceFileResp`）：

```json
{
  "id": "...", "app_id": "...", "conversation_id": "...",
  "path": "/workspace/report.md", "revision": 2, "mime_type": "text/markdown",
  "size_bytes": 1024, "content_sha256": "...",
  "created_by": "...", "created_at": "...", "updated_at": "..."
}
```

预览响应追加 `content`（超限时为 `null`）、`download_url`、`truncated`。

**重要**：响应里的 `conversation_id` 实际是 Workspace **作用域 ID**（`uuid5(NAMESPACE_URL, "my-dify:agent-workspace:v1:{account}:{app}:{account}")`），**不是** `Conversation.id`——它是沿用历史列名的实现细节。

**下载**：`302` 跳转到 COS 签名 URL，有效期 `WORKSPACE_DOWNLOAD_URL_TTL_SECONDS`（默认 300 秒）。

**作用域差异（重要）**：

| 路径族 | 作用域账号 |
| --- | --- |
| `/apps/<app_id>/workspace/...` | `app.account_id`（应用所有者） |
| `/web-apps/<token>/workspace/...` | `current_user.id`（**当前登录账号**） |
| Agent 运行时读写 | `app.account_id`（应用所有者） |

含义：Agent 产出的文件总是落在 `(应用所有者, app_id)` 这个空间里，所以**只有登录账号恰好是应用所有者时**，WebApp 路径族才看得到 Agent 的文件；其他账号通过 WebApp 路径访问会落在自己的空间（通常为空）。这不是"终端用户私有空间"——WebApp 路由的身份是账号，不是 `EndUser`。

---

## 10. 长期记忆

平台信封 + 登录态。要求应用已启用长期记忆，否则 `该应用未启用长期记忆`（`forbidden`）。

| 方法 | 路径 | 启用标记来源 |
| --- | --- | --- |
| `GET` | `/apps/<uuid:app_id>/memory/files` | **草稿**配置 |
| `GET` | `/apps/<uuid:app_id>/memory/file` | 草稿配置 |
| `GET` | `/apps/<uuid:app_id>/memory/file/download` | 草稿配置 |
| `POST` | `/apps/<uuid:app_id>/memory/file` | 草稿配置 |
| `DELETE` | `/apps/<uuid:app_id>/memory/file` | 草稿配置 |
| `GET` | `/web-apps/<string:token>/memory/files` | **发布**配置 |
| `GET` | `/web-apps/<string:token>/memory/file` | 发布配置 |
| `GET` | `/web-apps/<string:token>/memory/file/download` | 发布配置 |
| `POST` | `/web-apps/<string:token>/memory/file` | 发布配置 |
| `DELETE` | `/web-apps/<string:token>/memory/file` | 发布配置 |

记忆作用域是 `(current_user.id, app_id)`——**与 Agent 运行时一致**，因此两个路由族作用域相同，唯一区别是启用标记取自草稿还是发布配置。

### 10.1 请求参数

| 接口 | 字段 | 约束 |
| --- | --- | --- |
| 列表 / 读取 / 下载 | `path`（query） | 必填，≤512 |
| 写入 | `path`（JSON body） | 必填，≤512 |
| | `content` | **必须存在且为字符串**，允许空串 |
| | `original_path` | 可选，≤512（重命名 / 移动） |
| | `expected_sha256` | 可选，正则 `^[a-f0-9]{64}$` |
| 删除 | `path` | 必填，≤512 |
| | `expected_sha256` | 可选，同正则 |

### 10.2 响应

列表：`data = {"items": [MemoryFileResp...]}`。

```json
{
  "path": "/memories/preferences/style.md",
  "size_bytes": 128,
  "created_at": "...",
  "updated_at": "...",
  "content_sha256": "...",
  "preview": "..."
}
```

读取与写入返回 `MemoryFileContentResp`（追加 `content` 全文）。
删除成功：`{"code":"success","message":"记忆文件已删除","data":{}}`。

### 10.3 下载

`GET .../memory/file/download?path=...` 返回**原始附件**（不是平台信封）：

- `Content-Type: text/plain`
- `Content-Disposition: attachment; filename="<path 的最后一段>"`
- 文件内容为 UTF-8 编码的记忆正文。

### 10.4 约束

| 项 | 值 |
| --- | --- |
| 单文件上限 | 64 KiB |
| 文件数上限 | 100 |
| 总量上限 | 1 MiB |
| `/memories/.index.md` | **系统生成且只读**；写入会失败（`记忆索引由系统生成，请编辑分类下的记忆文件`） |
| 分类目录 | `preferences` / `background` / `experience` |
| 并发控制 | `expected_sha256` 乐观校验；Store 侧 `pg_advisory_xact_lock` + `lock_timeout=5000ms` |

---

## 11. 测评

平台信封 + 登录态。

| 方法 | 路径 | 限流 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/apps/<uuid:app_id>/evaluation-sources` | 无 | 可测评的历史任务（页码分页） |
| `GET` | `/apps/<uuid:app_id>/evaluation-sources/<uuid:run_id>` | 无 | 单个来源的完整送评文本 |
| `POST` | `/apps/<uuid:app_id>/evaluations` | `chat`（12/60s，令牌桶） | 提交测评 |
| `GET` | `/apps/<uuid:app_id>/evaluations` | 无 | 测评历史（页码分页，省略长文本） |
| `GET` | `/apps/<uuid:app_id>/evaluations/<uuid:evaluation_id>` | 无 | 测评详情 |

### 11.1 来源可见范围

只能测评**自己名下**、**已结束**（`completed` / `failed` / `cancelled`）、来源为 `debugger` / `creator_task` 的任务。
列表按 `created_at DESC, id DESC` 排序。不可见：`任务不存在、尚未结束或无权测评`。
应用不存在或非本人：`应用不存在或无权访问`。

`EvaluationSourceResp`：

```json
{
  "run_id": "...", "conversation_id": "...",
  "query": "...", "answer": "...",
  "status": "completed", "invoke_from": "creator_task",
  "created_at": "...", "finished_at": "...",
  "scope": "仅本轮任务描述与最终回答；不包含此前对话、附件内容、工具轨迹及外部系统核验。",
  "execution": null
}
```

列表接口的 `query` 会截断为 160 字符预览；详情接口返回全文并带 `answer`。

### 11.2 `POST /apps/<uuid:app_id>/evaluations`

请求体（JSON，`extra="forbid"`）：

```json
{
  "run_id": "...",
  "request_id": "客户端幂等 ID（可选 UUID）",
  "judge": {"provider": "openai", "model": "gpt-4o", "provider_connection_id": "..."},
  "mode": "historical",
  "max_iterations": 3,
  "evidence_rules": [
    {"kind": "contains", "value": "结论"},
    {"kind": "json_keys", "value": "title,summary"},
    {"kind": "file_exists", "value": "/workspace/report.md"}
  ],
  "criteria": ["结论有数据支撑", "给出了可执行的下一步"]
}
```

| 字段 | 约束 |
| --- | --- |
| `run_id` | 必填 UUID |
| `request_id` | 可选 UUID；提供时用于幂等 |
| `judge` | 必填；只能引用模型（provider / model / provider_connection_id） |
| `mode` | `historical`（默认）/ `execute` |
| `max_iterations` | 严格整数 1–5，默认 3 |
| `evidence_rules` | ≤8 条；`kind` ∈ `contains` / `json_keys` / `file_exists`；`value` 1–500 |
| `evidence_rules[].value`（`file_exists`） | 必须是 `/` 开头的绝对虚拟路径，不含 `..`，不含 `\` |
| `evidence_rules[].value`（`json_keys`） | 逗号分隔的键名，每个非空 |
| `criteria` | 1–12 条，每条 1–500 字符，**不可重复** |

**通用校验失败**（任何字段不合法）统一返回：

```json
{"code":"validate_error","message":"请选择历史任务、评分模型，并填写 1–12 条不重复标准，每条最多 500 字","data":{"evaluation":["..."]}}
```

其他业务失败：

| 场景 | 文案 |
| --- | --- |
| 材料超长（`query` + `answer` > 48000 字） | `本轮文本超过 48000 字，暂不支持测评；不会截断后评分` |
| `request_id` 复用但内容不同 | `提交标识已用于不同的测评，请重新提交` |
| `execute` 模式无草稿配置 | `当前 Agent 没有草稿配置，请先保存 Agent 配置` |
| 队列不可用 | 记录写入后置 `error`，文案 `测评队列暂不可用，请稍后新建测评。` |
| 超时（>900 秒） | `测评等待或执行超过 15 分钟，请检查后台队列后新建测评。` |
| 评分模型失败 | `评分模型调用失败或返回无效评分，请检查模型后新建测评。` |
| 数据库结构未升级 | `测评数据库结构尚未更新，请联系管理员完成数据库迁移后重试。` |

成功响应 `AgentEvaluationResp`：

```json
{
  "id": "...", "app_id": "...", "run_id": "...",
  "source_snapshot": {...},
  "criteria": ["..."],
  "judge_config": {"provider": "...", "model": "...", "provider_connection_id": "...", "parameters": {}},
  "evaluator_version": "answer-rubric-v3",
  "status": "queued",
  "result": null,
  "error_message": "",
  "created_at": "...", "finished_at": null
}
```

- `evaluator_version`：`historical` → `answer-rubric-v3`；`execute` → `rubric-runtime-v1`。
- `status` ∈ `queued` / `running` / `completed` / `error`。
- **提交是异步的**：HTTP 立即返回 `queued`，评分由 Celery 完成。

### 11.3 评分结果 `result`

`historical` 模式：

```json
{
  "checks": [{
    "criterion_id": 1,
    "verdict": "passed",
    "reason": "...",
    "evidence_source": "answer",
    "evidence_quote": "...",
    "suggestion": "...",
    "score": 1.0
  }],
  "summary": {"passed": 1, "failed": 0, "insufficient": 0, "total": 1,
              "pass_rate": 100.0, "score": 1.0, "coverage": 1.0,
              "evaluated": 1, "evaluated_pass_rate": 100.0, "outcome": "passed"}
}
```

- `verdict` ∈ `passed` / `failed` / `insufficient_evidence`。
- `evidence_source` ∈ `query` / `answer` / `none`。
- 档位与分数必须一致：`passed` → `1.0`；`failed` → `0.0` 或 `0.5`；`insufficient_evidence` → `null`。不一致：`评分档位与结论不一致`。
- `passed` 必须给出非空 `evidence_quote`，且引用必须是原文子串。
- `outcome`：`failed` 优先于 `insufficient_evidence` 优先于 `passed`。

`execute` 模式：`result = {"runtime": {...}}`，含 `phase` / `status` / `accepted` / `rounds` / `final_answer` / `files` 等。

### 11.4 `GET /apps/<uuid:app_id>/evaluations`

页码分页。响应 `data = {list, total, current_page, page_size}`；列表项的 `result` 被裁剪为只保留 `runtime.phase/status/accepted`，`rounds` 置空数组，且 `source_snapshot.answer` 被排除。

### 11.5 `GET /apps/<uuid:app_id>/evaluations/<uuid:evaluation_id>`

返回完整快照与逐项结论。不存在：`测评记录不存在`。

---

## 12. 限流汇总

| 接口 | 配置键 | 上限 | 策略 | 装饰器位置 |
| --- | --- | --- | --- | --- |
| `POST /assistant-agent/conversations` | `user` | 40 / 60s | 滑动窗口 | `@login_required` **外层**（认证前即计数） |
| `POST /apps/<id>/evaluations` | `chat` | 12 / 60s | 令牌桶 | `@login_required` 内层 |
| `POST /apps`、`POST /apps/<id>`、`POST /apps/<id>/copy`、`POST /apps/<id>/draft-app-config`、`.../fallback-history` | `user` | 40 / 60s | 滑动窗口 | — |
| `POST /apps/<id>/publish`、`.../cancel-publish`、`.../regenerate-web-app-token`、`POST /apps/<id>/delete` | `sensitive` | 3 / 60s | 固定窗口 | — |
| `GET /apps/<id>`、`GET /apps/<id>/draft-app-config`、`.../published-subagents`、`.../published-config` | `lightweight` | 60 / 60s | 固定窗口 | — |
| `GET /apps`、`GET /apps/<id>/publish-histories` | `database_intensive` | 25 / 60s | 滑动窗口 | — |

**没有限流**的 Agent 接口：工作台五件套（全部入口）、`/agent-runs/*/events`、`/agent-protocol/*`（全部 5 个）、`/task` 系列、Skills、Workspace、Memory、测评的四个只读接口。

触发限流：HTTP **429** + `Retry-After`。

**降级行为**：Redis 不可用时限流器直接放行当前请求，并记录日志 `限流器不可用，跳过限流检查`。

---

## 13. 错误文案速查

### 13.1 平台信封接口

| 文案 | 触发接口 |
| --- | --- |
| `应用不存在、未发布或无权访问` | 任务中心、开放 API 创建会话 |
| `会话不存在或无权访问` | 任务中心会话详情/更新/删除 |
| `应用不存在或无权访问` | 测评 |
| `任务不存在、尚未结束或无权测评` | 测评来源详情 |
| `测评记录不存在` | 测评详情 |
| `该应用未启用长期记忆` | Memory 全部接口 |
| `Skill 模板不存在` | `GET /skills/<name>` |
| `Skill 不存在或未内置: {name}` | 草稿配置更新 |
| `Skill 版本不匹配: {name}` | 草稿配置更新 |
| `path 不能为空` | Workspace / Memory 读取 |
| `content 必须是字符串` | Workspace / Memory 写入 |
| `草稿配置不能为空` | 草稿配置更新 |
| `配置字段不能为 null: {keys}` | 草稿配置更新 |
| `发布 Agent 不能引用当前应用自身` | 草稿配置更新（已发布） |
| `发布 Agent 子 Agent 引用存在循环` | 同上 |
| `发布 Agent 子 Agent 嵌套层级不能超过 32` | 同上 |
| `更新应用草稿配置成功` | 草稿配置更新成功 |
| `发布/更新应用配置成功` | 发布成功 |
| `Workspace 文件已删除` | Workspace 删除成功 |
| `记忆文件已删除` | Memory 删除成功 |
| `删除会话成功` | 任务中心删除会话成功 |

### 13.2 工作台接口（`{type:"error"}` 的 `message`）

见 §3.1 表格。

### 13.3 内部 SDK 接口（`{"detail": ...}`）

| 文案 | 状态 |
| --- | --- |
| `内部 Agent Protocol 凭证无效` | 401 |
| `内部 Agent Protocol 凭证已过期` | 401 |
| `内部 Agent Protocol 调用不在有效应用范围内` | 403 |
| `内部 Agent Protocol graph_id 不在授权范围内` | 403 |
| `内部 Agent Protocol 会话不存在或无权访问` | 404 |
| `内部 Agent Protocol Run 不存在` | 404 |
| `内部 Agent Protocol 请求格式错误: {exc}` | 422 |
| `Run 必须且只能提供 input 或 command` | 422 |
| `后台 Run 缺少 input` | 422 |
| `后台 Run 输入格式错误: {exc}` | 422 |
| `内部 Agent Protocol 请求失败` | 500（兜底） |
| `内部 Agent Protocol 密钥未配置` | 签发侧 `FailException` |

---

## 14. 前端调用约定（交叉验证）

`my-dify-ui` 的实际调用方式（`src/services/agent-workbench.ts`、`workbench-sse-fetch.ts`）与本文档一致：

- 路径按入口前缀拼接：`app` → `/apps/<appId>`、`web_app` → `/web-apps/<token>`、`service_api` → `/openapi/apps/<appId>`、`creator_task` → `/tasks/apps/<appId>`。
- `run.start` / `input.respond` 由 **LangGraph SDK** 发出（走 `/commands`），只有 `input.reopen` 由前端直接 POST。
- SSE 断点续传使用 `Last-Event-ID` 请求头，`run_id` 由客户端注入请求体。
- 前端 `channels` 会重写为包含 `custom`，并置 `depth: undefined`。
- 事件投影只处理 `values` / `messages` / `tools` / `lifecycle` / `custom`；`input` / `checkpoints` 虽在订阅集合中但**不做投影**——待填表单通过 `values.pending_inputs` 到达前端。
- `client.threads.getHistory` 被实现为返回空数组（历史走 `/state`）。
