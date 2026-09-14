# LLMOps 项目 Agent API 文档

> 本文档依据当前代码实现整理（`internal/router`、`internal/handler`、`internal/schema`、`pkg/response`），不引用任何历史设计文档或仓库内 Markdown。
> 所有路径均为 **根路径**：`my_dify` 与 `openapi` 两个蓝图的 `url_prefix` 都是空串。

Agent 相关接口的响应格式**共有三种**，不能混用判断逻辑：

1. **平台信封**（应用配置、Skill、Workspace、长期记忆、测评、Creator 任务中心）：统一以 JSON 返回，并包含 3 个字段 `code`、`data` 和 `message`，分别代表 `业务状态码`、`业务数据` 和 `接口附加信息`。
2. **Agent Protocol 原始 JSON**（工作台命令、状态、模型设置、取消、运行事件、`/agent-protocol/*`）：**不包装** `code`/`message`，直接返回业务对象。
3. **SSE 流式事件**（所有 `/stream`）：以 `text/event-stream` 逐条输出事件。

`业务状态码`共有 7 种，其中只有 `success(成功)` 代表业务操作成功，其他 6 种状态均代表失败，并且失败时会附加相关的信息：`fail(通用失败)`、`not_found(未找到)`、`unauthorized(未授权)`、`forbidden(无权限)`、`validate_error(数据验证失败)` 和 `too_many_requests(请求过于频繁)`。

平台信封接口示例：

```json
{
    "code": "success",
    "data": {
        "id": "5e7834dc-bbca-4ee5-9591-8f297f5acded"
    },
    "message": "修改Agent智能体应用成功"
}
```

**业务异常统一走全局异常处理器，HTTP 状态码固定 200**（仅限流 `429` 例外）。因此**不要用 HTTP 状态码判断平台信封接口的成败**，必须读取 `code`。

未归类的异常在 `debug` 或 `FLASK_ENV=development` 时原样抛出（便于本地排障），否则返回 `{"code": "fail", "message": "服务器内部错误，请稍后重试", "data": {}}`；数据库连接类异常固定返回 `{"code": "fail", "message": "数据库连接异常，请稍后重试", "data": {}}`。

`validate_error_json` 会把第一条字段错误提到 `message`，并把全部字段错误放进 `data`：

```json
{
    "code": "validate_error",
    "message": "path 不能为空",
    "data": {
        "path": ["path 不能为空"]
    }
}
```

带有分页数据的平台信封接口会在 `data` 内固定传递 `list` 和 `paginator` 字段（如 `GET:/apps`），`paginator` 内存在 4 个字段：`current_page(当前页数)`、`page_size(每页数据条数)`、`total_page(总页数)`、`total_record(总记录条数)`；**测评与任务中心历史列表不使用该结构**，详见对应接口说明。

如果接口需要授权，需要在 `headers` 中添加 `Authorization`，并附加 `access_token` 即可完成授权登录，示例：

```json
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE3MTY0NTY3OTgsImlzcyI6ImxsbW9wcyIsInN1YiI6ImM5MDljMWRiLWIyMmUtNGZlNi04OGIyLWIyZTkxZWFiMWE3YiJ9.JDAtWDBBGiXa_XFihfopRe4Cz-RQ9_TAcno9w81tNbE
```

平台共两种凭证模型：`my_dify` 蓝图使用登录态 JWT，得到 `Account`；`openapi` 蓝图使用平台 API Key（格式为 `llmops-v1/` + `secrets.token_urlsafe(48)`），同样解析为 `Account`。**WebApp 路径中的 `<token>` 不是凭证**，仅用于定位应用，仍需登录态。

---

## 01. Agent 工作台模块

Agent 工作台是平台统一的 Agent Protocol 运行时接口，**五个业务入口共用同一套五个端点**，端点后缀完全一致，仅路径前缀与固定的调用来源不同：

| 入口 | 路径前缀 | 调用来源 | 会话授权方式 |
|---|---|---|---|
| 调试页 | `/apps/:app_id/conversations/:conversation_id` | `debugger` | `AppService.get_debugger_conversation` |
| WebApp | `/web-apps/:token/conversations/:conversation_id` | `web_app` | `WebAppService.get_owned_web_conversation` |
| 辅助 Agent | `/assistant-agent/conversations/:conversation_id` | `assistant_agent` | `AssistantAgentService.get_owned_conversation` |
| Creator 任务 | `/tasks/apps/:app_id/conversations/:conversation_id` | `creator_task` | `CreatorTaskService.get_owned_conversation` |
| 开放 API | `/openapi/apps/:app_id/conversations/:conversation_id` | `service_api` | `ConversationService.get_service_api_conversation` |

五个端点后缀：

| 方法 | 后缀 | 说明 |
|---|---|---|
| `POST` | `/commands` | 提交运行命令 |
| `POST` | `/stream` | 订阅 SSE 执行事件流 |
| `GET` | `/state` | 读取会话状态快照 |
| `GET` | `/model-settings` | 读取模型设置 |
| `POST` | `/runs/:run_id/cancel` | 取消指定运行 |

调用身份（`account_id`、`created_by`、`end_user_id`、`invoke_from`）全部由服务端根据登录态或凭证推导，**命令体与请求体无法覆盖**：

| 入口 | `account_id` | `created_by` | `end_user_id` |
|---|---|---|---|
| 调试页 | 当前登录账号 | 当前登录账号 | 不传递 |
| WebApp | 当前登录账号 | 终端用户 id | 终端用户 id |
| 辅助 Agent | 当前登录账号 | 当前登录账号 | 不传递 |
| Creator 任务 | 当前登录账号 | 当前登录账号 | 不传递 |
| 开放 API | API Key 绑定账号 | 会话创建者 | 会话创建者 |

### 1.1 提交运行命令

- **接口说明**：该接口是工作台唯一的写入入口，通过 `method` 区分三种命令：`run.start(启动新任务)`、`input.respond(提交表单答案)`、`input.reopen(恢复被取消的表单)`。命令的鉴权、幂等和并发冲突都由服务端处理，客户端不需要先查询状态再提交。**该接口固定返回 HTTP 200**，命令级失败也通过响应体中的 `type: "error"` 表达，不改变 HTTP 状态码。

- **接口信息**：`授权`+`POST:/apps/:app_id/conversations/:conversation_id/commands`（其余四个入口替换路径前缀即可）

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `conversation_id -> uuid`：路由参数，会话 id，类型为 uuid。
    - `id -> int`：可选参数，客户端命令序号，类型为整型，会原样回显在响应 `id` 中；不传时响应 `id` 固定为 0。
    - `type -> str`：可选参数，命令类型标识，类型为字符串，服务端不校验该字段。
    - `method -> str`：必填参数，命令名称，枚举值为 `run.start`、`input.respond`、`input.reopen`。
    - `params -> dict`：必填参数，命令参数，结构随 `method` 变化，详见下方三张参数表。
  - 请求参数（`method = run.start`）：
    - `input -> dict`：与 `params` 同级的业务输入容器；服务端会先取 `params.input`，没有该键时直接把 `params` 当作输入对象，因此 `{"params": {"query": "..."}}` 与 `{"params": {"input": {"query": "..."}}}` 等价。
      - `messages -> list[dict]`：消息列表，只有**最后一条**会被使用，类型为字典列表。
        - `id -> str`：可选参数，消息标识，长度不超过 255，用于命令幂等去重。
        - `type -> str` / `role -> str`：可选参数，角色标识，支持 `human` 和 `user`。
        - `content -> any`：消息正文，字符串或内容块数组；数组时只拼接 `type` 为 `text` 或未标注 `type` 的文本片段。
      - `query -> str`：用户提问文本；`messages` 为空时使用该字段。与 `messages` 至少提供一个，且去除首尾空白后不能为空。
      - `planning_enabled -> bool`：可选参数，是否启用任务规划，默认 `false`。
      - `reasoning_effort -> str`：可选参数，推理等级，长度 1–64，需在该模型的可用等级内。
      - `model_selection -> dict`：可选参数，本轮切换模型，包含 `provider`、`model`、`provider_connection_id` 三个字段；不接受凭证、地址或任意参数。
      - `image_urls -> list[str]`：可选参数，图片 URL 列表，最多 10 条；不传或传 `null` 时按空列表处理。
      - `file_ids -> list[uuid]`：可选参数，附件 id 列表，最多 5 条且不能重复。
      - `selected_skills -> list[str]`：可选参数，本轮启用的 Skill 名称，最多 20 条，名称长度 1–64；不传或传 `null` 时按空列表处理，去重后不能为空项。
      - `assistant_id -> str`：可选参数，LangGraph SDK 兼容字段，服务端**接受但忽略**。
  - 请求参数（`method = input.respond`）：
    - `responses -> list[dict]`：必填参数，答案批次，数量 1–40，同一中断不能出现两次。
      - `namespace -> list[str]`：可选参数，**只接受空数组**（`max_length=0`），非空会被拒绝。
      - `interrupt_id -> str`：必填参数，中断标识，长度 1–255，必须与状态接口返回的待填表单一一对应。
      - `response -> dict`：必填参数，该表单的答案对象，键为字段名。
    - 兼容形式：当 `params` 直接是 `{interrupt_id, response, namespace}` 时，服务端自动包装为 `{"responses": [...]}`，用于兼容官方 SDK 的单条提交。
  - 请求参数（`method = input.reopen`）：
    - `run_id -> uuid`：必填参数，被取消运行的 id，必须是当前会话最新 Run。
    - `cancellation_sequence -> int`：必填参数，取消事件序号，必须大于 0，用于防止迟到的请求复活后来停止的任务。
  - 响应参数（成功，`type = "success"`）：
    - `type -> str`：固定为 `success`。
    - `id -> int`：回显请求中的命令序号，请求未传时为 0。
    - `result -> dict`：命令结果。
      - `run_id -> uuid`：本次命令对应的运行 id，类型为 uuid。重复提交同一命令会返回**同一个** `run_id`。
    - `meta -> dict | null`：附加元数据，仅包含 `applied_through_seq(已生效事件序号)`，命令不产生事件序号时为 `null`。
  - 响应参数（失败，`type = "error"`）：
    - `type -> str`：固定为 `error`。
    - `id -> int | null`：回显请求中的命令序号，请求未传时为 `null`。
    - `error -> str`：错误类型，当前实现固定为 `invalid_argument`。
    - `message -> str`：可直接展示给用户的失败原因，取值见文末错误文案速查。
    - `field_errors -> dict | null`：字段级错误，结构为 `{interrupt_id: {field_name: message}}`，仅在表单答案校验失败时出现。

- **请求示例**：

  ```bash
  POST:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/conversations/2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8/commands
  
  {
      "id": 1,
      "method": "run.start",
      "params": {
          "input": {
              "messages": [
                  {
                      "id": "local-1714053834",
                      "type": "human",
                      "content": "能详细讲解下LLM是什么吗？"
                  }
              ]
          }
      }
  }
  ```

- **请求示例（提交表单答案）**：

  ```bash
  POST:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/conversations/2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8/commands
  
  {
      "id": 2,
      "method": "input.respond",
      "params": {
          "responses": [
              {
                  "interrupt_id": "call_9f2c1a",
                  "response": {
                      "report_scope": "本季度",
                      "include_charts": ["趋势图", "对比图"]
                  }
              }
          ]
      }
  }
  ```

- **响应示例（成功）**：

  ```json
  {
      "type": "success",
      "id": 1,
      "result": {
          "run_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70"
      },
      "meta": {
          "applied_through_seq": 3
      }
  }
  ```

- **响应示例（失败）**：

  ```json
  {
      "type": "error",
      "id": 1,
      "error": "invalid_argument",
      "message": "请先完成当前表单"
  }
  ```

### 1.2 订阅执行事件流

- **接口说明**：该接口以 SSE 方式持续输出指定运行的执行事件，涵盖消息增量、状态快照、工具调用、生命周期、上下文观测和待填表单。接口支持断线续传：客户端把最后收到的事件 id 放入 `Last-Event-ID` 请求头，或用请求体 `since` 指定序号，服务端会从该位置之后继续推送，不会重复已确认的事件。

- **接口信息**：`授权`+`POST:/apps/:app_id/conversations/:conversation_id/stream`（其余四个入口替换路径前缀即可）

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `conversation_id -> uuid`：路由参数，会话 id，类型为 uuid。
    - `run_id -> uuid`：必填参数，需要订阅的运行 id，类型为 uuid。
    - `since -> int`：可选参数，起始事件序号，默认为 0，最小值为 0。
    - `channels -> list[str]`：可选参数，事件通道过滤，枚举值为 `messages`、`values`、`tools`、`lifecycle`、`input`、`checkpoints`、`custom`；空数组表示不过滤。其中 `input` 映射到实际通道 `input.requested`，`checkpoints` 仅为兼容官方 SDK 而接受，**当前不发布该类事件**。
    - `namespaces -> list[list[str]]`：可选参数，命名空间前缀过滤，满足任一前缀即保留；空数组表示不过滤。
    - `depth -> int`：可选参数，命名空间深度上限，最小值为 0；仅保留命名空间长度不超过该值的事件。
  - 请求头参数：
    - `Last-Event-ID -> str`：可选参数，断线续传游标。规则为：普通形式 `<run_id>:<seq>` 取 `seq`；`<run_id>:cursor-reset:<n>` 取 `n - 1`；解析失败时忽略该请求头。解析成功后取 `max(请求体 since, 解析值)`。
  - 流式事件参数：
    - `event -> str`：SSE 事件名，取自事件的 `method`，例如：`messages(消息)`、`values(状态)`、`tools(工具)`、`lifecycle(生命周期)`、`custom(上下文观测)`、`input.requested(待填表单)`。
    - `id -> str`：SSE 事件 id，格式为 `<run_id>:<seq>`，可直接作为下次请求的 `Last-Event-ID`。
    - `data -> dict`：事件正文，为一个完整的 `WorkbenchEvent` 对象，结构见文末「工作台事件数据结构」。

  首次建立连接时会先发送一行 `: connected` 注释；空闲超过 15 秒会发送一行 `: heartbeat` 心跳注释，用于保持连接与探测断线。**响应为 `text/event-stream`，不包含平台信封。**

- **请求示例**：

  ```bash
  POST:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/conversations/2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8/stream
  Last-Event-ID: 8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70:12
  
  {
      "run_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70",
      "since": 0,
      "channels": ["messages", "values", "tools", "lifecycle", "custom"],
      "depth": 3
  }
  ```

- **响应示例**：

  ```
  : connected

  id: 8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70:1
  event: lifecycle
  data: {"type":"event","event_id":"8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70:1","seq":1,"method":"lifecycle","params":{"namespace":[],"timestamp":1714053834000,"node":null,"data":{"event":"running","status":"running","phase":"preparing","message_id":"c0759ca8-2d35-4480-83a8-1f41f29d1401","input_message_id":"local-1714053834"},"metadata":{"my_dify":{"source":"workbench"}}}}

  id: 8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70:2
  event: messages
  data: {"type":"event","event_id":"8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70:2","seq":2,"method":"messages","params":{"namespace":[],"timestamp":1714053834200,"node":null,"data":{"event":"message-start","role":"ai","id":"c0759ca8-2d35-4480-83a8-1f41f29d1401:assistant"},"metadata":{"my_dify":{"source":"agent_v2","source_seq":4}}}}

  : heartbeat

  id: 8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70:3
  event: messages
  data: {"type":"event","event_id":"8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70:3","seq":3,"method":"messages","params":{"namespace":[],"timestamp":1714053834500,"node":null,"data":{"event":"content-block-delta","id":"c0759ca8-2d35-4480-83a8-1f41f29d1401:assistant","delta":{"type":"text-delta","text":"LLM 即 Large Language Model，大语言模型。"}},"metadata":{"my_dify":{"source":"agent_v2","source_seq":5}}}}
  ```

### 1.3 获取会话状态快照

- **接口说明**：该接口用于在水合页面或断线重连时读取当前会话的完整状态，包含最近 20 条消息、运行摘要列表、待填表单和当前运行的元数据。**该接口只读取已提交的快照**，与 SSE 订阅使用同一数据源，因此不会出现状态与事件不一致。执行孤儿收敛：若发现本进程未持有的 `running` 运行（例如进程重启），会在此处将其标记为失败。

- **接口信息**：`授权`+`GET:/apps/:app_id/conversations/:conversation_id/state`（其余四个入口替换路径前缀即可）

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `conversation_id -> uuid`：路由参数，会话 id，类型为 uuid。
  - 响应参数：
    - `values -> dict`：会话状态投影，只包含白名单字段，不含模型调用配置与内部表单恢复信息。
      - `messages -> list[dict]`：最近 20 条对话消息，按时间正序，类型为字典列表。
        - `id -> str`：消息 id。人类消息为 `Run.input_message_id` 或 `<message_id>:human`；AI 消息为 `<message_id>:assistant`。
        - `type -> str`：消息角色，取值为 `human` 或 `ai`。
        - `content -> any`：消息正文。
        - `additional_kwargs -> dict`：附加字段，AI 消息中包含 `run_id`。
      - `message_page -> dict`：消息分页信息，结构为 `{has_more: bool, next_before: uuid | null}`；需要更早的历史时使用 `next_before` 作为游标另行拉取。
      - `runs -> list[dict]`：会话内的运行摘要列表，不含执行事件。
        - `run_id -> uuid`：运行 id。
        - `message_id -> uuid`：承载本轮问答的消息 id。
        - `input_message_id -> str | null`：客户端输入消息标识。
        - `status -> str`：运行状态，取值为 `running`、`waiting_input`、`completed`、`failed`、`cancelled`。
        - `last_event_sequence -> int`：已持久化的事件序号。
        - `created_at -> str`：创建时间，ISO 8601 UTC 字符串。
        - `started_at -> str | null`：首次开始执行的时间。
        - `finished_at -> str | null`：进入终态的时间；等待用户输入时不设置。
        - `error -> str | null`：仅 `failed` 时返回可展示的失败原因。
      - `todos -> list`：任务规划列表，未启用规划时不存在该键。
      - `files -> list`：会话文件列表，没有文件时不存在该键。
      - `async_tasks -> dict`：异步子 Agent 任务表，键为任务 id，值为任务状态；原字段 `thread_id` 已投影为 `conversation_id`。
      - `pending_inputs -> list[dict]`：待填表单列表，元素结构为 `{interrupt_id, namespace, payload}`，`payload` 为表单定义。
      - `input_history -> list[dict]`：已提交的答案历史。
      - `input_recovery -> dict | null`：可恢复的表单标识，结构为 `{run_id, cancellation_sequence}`；没有可恢复表单时为 `null`。
    - `next -> list[str]`：需要继续执行的节点；**仅在运行状态为 `running` 时返回 `["agent"]`**，其余状态返回空数组。
    - `tasks -> list[dict]`：待处理任务，仅在状态为 `waiting_input` 时生成，否则为空数组。
      - `id -> str`：任务 id，等于中断 id。
      - `name -> str`：任务名称，固定为 `ask_user`。
      - `interrupts -> list[dict]`：中断列表，元素结构为 `{id, value, ns}`，`value` 为表单定义。
    - `metadata -> dict`：当前运行元数据。
      - `run_id -> uuid | null`：当前运行 id；会话没有运行时为 `null`。
      - `status -> str`：当前状态；会话没有运行时为 `idle`。
      - `last_event_sequence -> int`：当前运行的事件序号；会话没有运行时为 0。

- **请求示例**：

  ```bash
  GET:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/conversations/2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8/state
  ```

- **响应示例**：

  ```json
  {
      "values": {
          "messages": [
              {
                  "id": "local-1714053834",
                  "type": "human",
                  "content": "能详细讲解下LLM是什么吗？",
                  "additional_kwargs": {}
              },
              {
                  "id": "c0759ca8-2d35-4480-83a8-1f41f29d1401:assistant",
                  "type": "ai",
                  "content": "LLM 即 Large Language Model，大语言模型。",
                  "additional_kwargs": {
                      "run_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70"
                  }
              }
          ],
          "message_page": {
              "has_more": false,
              "next_before": null
          },
          "runs": [
              {
                  "run_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70",
                  "message_id": "c0759ca8-2d35-4480-83a8-1f41f29d1401",
                  "input_message_id": "local-1714053834",
                  "status": "waiting_input",
                  "last_event_sequence": 12,
                  "created_at": "2026-09-14T11:23:54Z",
                  "started_at": "2026-09-14T11:23:54Z",
                  "finished_at": null,
                  "error": null
              }
          ],
          "pending_inputs": [
              {
                  "interrupt_id": "call_9f2c1a",
                  "namespace": [],
                  "payload": {
                      "title": "补充周报范围",
                      "description": "",
                      "fields": [
                          {
                              "name": "report_scope",
                              "label": "统计范围",
                              "type": "select",
                              "required": true,
                              "options": ["本周", "本季度"],
                              "description": "",
                              "placeholder": "",
                              "allow_other": false,
                              "allow_skip": false,
                              "allow_unavailable": true,
                              "allow_delegate": false,
                              "min_length": 0,
                              "max_length": 4000,
                              "min_value": null,
                              "max_value": null,
                              "integer_only": false
                          }
                      ]
                  }
              }
          ],
          "input_history": [],
          "input_recovery": null
      },
      "next": [],
      "tasks": [
          {
              "id": "call_9f2c1a",
              "name": "ask_user",
              "interrupts": [
                  {
                      "id": "call_9f2c1a",
                      "value": {
                          "title": "补充周报范围",
                          "description": "",
                          "fields": []
                      },
                      "ns": []
                  }
              ]
          }
      ],
      "metadata": {
          "run_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70",
          "status": "waiting_input",
          "last_event_sequence": 12
      }
  }
  ```

### 1.4 获取工作台模型设置

- **接口说明**：该接口用于读取当前应用在当前入口下可用的模型目录、当前选中的模型与推理等级。**只有创建者入口（调试页、Creator 任务）会返回可切换的模型目录**，WebApp、辅助 Agent 与开放 API 入口返回空目录，仅暴露当前生效的模型标识。

- **接口信息**：`授权`+`GET:/apps/:app_id/conversations/:conversation_id/model-settings`（其余四个入口替换路径前缀即可）

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `conversation_id -> uuid`：路由参数，会话 id，类型为 uuid。
  - 响应参数：
    - `model -> str`：应用配置中的模型名称；未配置时为空字符串。
    - `provider -> str`：应用配置中的模型提供方；未配置时为空字符串。
    - `selected_model -> dict | null`：当前生效的模型引用，结构为 `{provider, model, provider_connection_id}`。
    - `model_override -> dict | null`：当前会话通过 `run.start` 临时切换的模型引用；没有切换时为 `null`。
    - `available_models -> list[dict]`：可切换的模型目录，类型为字典列表，非创建者入口为空数组。
      - `provider -> str`：模型提供方标识。
      - `model -> str`：模型标识。
      - `provider_connection_id -> str`：模型连接 id。
      - `label -> str`：模型展示名称。
      - `connection_name -> str`：模型连接的展示名称。
      - `reasoning_levels -> list[str]`：该模型支持的推理等级。
      - `default_reasoning_effort -> str | null`：默认推理等级。
    - `reasoning_levels -> list[str]`：当前模型的可用推理等级。
    - `default_reasoning_effort -> str | null`：当前模型的默认推理等级。
    - `selected_reasoning_effort -> str | null`：当前会话已选择的推理等级。

- **请求示例**：

  ```bash
  GET:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/conversations/2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8/model-settings
  ```

- **响应示例**：

  ```json
  {
      "model": "gpt-4o-mini",
      "provider": "openai",
      "selected_model": {
          "provider": "openai",
          "model": "gpt-4o-mini",
          "provider_connection_id": "1550b71a-1444-47ed-a59d-c2f080fbae94"
      },
      "model_override": null,
      "available_models": [
          {
              "provider": "openai",
              "model": "gpt-4o-mini",
              "provider_connection_id": "1550b71a-1444-47ed-a59d-c2f080fbae94",
              "label": "GPT-4o mini",
              "connection_name": "默认 OpenAI 连接",
              "reasoning_levels": [],
              "default_reasoning_effort": null
          }
      ],
      "reasoning_levels": [],
      "default_reasoning_effort": null,
      "selected_reasoning_effort": null
  }
  ```

### 1.5 取消指定运行

- **接口说明**：该接口用于停止指定运行。取消**不是状态位**：等待表单的运行会立即变为 `cancelled`，执行中的运行仍为 `running`，只记录取消请求时间，由执行线程在安全检查点自行收尾。因此响应中的 `status` 是取消请求发出后的**真实状态**，调用方需要继续订阅事件流等待终态。

- **接口信息**：`授权`+`POST:/apps/:app_id/conversations/:conversation_id/runs/:run_id/cancel`（其余四个入口替换路径前缀即可）

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `conversation_id -> uuid`：路由参数，会话 id，类型为 uuid。
    - `run_id -> uuid`：路由参数，需要取消的运行 id，类型为 uuid。
  - 响应参数：
    - `cancel_requested -> bool`：本次请求是否新受理了取消；运行已是终态时为 `false`。
    - `run_id -> uuid | null`：目标运行 id。
    - `status -> str`：取消请求后的真实运行状态。

- **请求示例**：

  ```bash
  POST:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/conversations/2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8/runs/8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70/cancel
  ```

- **响应示例**：

  ```json
  {
      "cancel_requested": true,
      "run_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70",
      "status": "running"
  }
  ```

### 1.6 创建辅助 Agent 会话

- **接口说明**：该接口用于获取或创建当前登录账号的辅助 Agent 会话。辅助 Agent 是平台内置的应用构建助手，**一个账号只维护一个会话**，重复调用返回同一个会话 id。

- **接口信息**：`授权`+`POST:/assistant-agent/conversations`

- **接口参数**：

  - 请求参数：无。
  - 响应参数：
    - `conversation_id -> uuid`：辅助 Agent 会话 id，类型为 uuid。

- **请求示例**：

  ```bash
  POST:/assistant-agent/conversations
  ```

- **响应示例**：

  ```json
  {
      "conversation_id": "2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8"
  }
  ```

### 1.7 创建开放 API 会话

- **接口说明**：该接口用于在指定应用下创建开放 API 会话。请求需要携带平台 API Key，创建出的会话归属由 `end_user_id` 指定的终端用户；不传该参数时使用 API Key 绑定账号。

- **接口信息**：`授权`+`POST:/openapi/apps/:app_id/conversations`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `end_user_id -> uuid`：可选参数，终端用户 id，必须是有效 UUID，否则返回 `end_user_id 必须是有效 UUID`。
  - 响应参数：
    - `conversation_id -> uuid`：创建的会话 id，类型为 uuid。

- **请求示例**：

  ```bash
  POST:/openapi/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/conversations
  
  {
      "end_user_id": "c0759ca8-2d35-4480-83a8-1f41f29d1401"
  }
  ```

- **响应示例**：

  ```json
  {
      "conversation_id": "2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8"
  }
  ```

---

## 02. Agent 运行记录模块

### 2.1 获取指定运行的分页执行事件

- **接口说明**：该接口用于查看一次运行已保存的执行过程，供观测详情页回放。接口按事件序号正序分页，并**应用与 SSE 相同的事件投影**，因此返回值是客户端可直接消费的公开事件。注意：投影会过滤掉部分内部事件，所以 `next_page` 的推进依据是原始持久化条数，返回条数可能少于 `page_size`。

- **接口信息**：`授权`+`GET:/agent-runs/:run_id/events`

- **接口参数**：

  - 请求参数：
    - `run_id -> uuid`：路由参数，运行 id，类型为 uuid。
    - `current_page -> int`：可选参数，当前页数，默认为 1，范围为 1–9999。
    - `page_size -> int`：可选参数，每页数据条数，默认为 20，范围为 1–50。
  - 响应参数：
    - `run_id -> uuid`：运行 id，类型为 uuid。
    - `events -> list[dict]`：该页的执行事件，元素为 `WorkbenchEvent` 对象，结构见文末「工作台事件数据结构」。
    - `next_page -> int`：下一页页码；没有下一页时为 `null`。

- **请求示例**：

  ```bash
  GET:/agent-runs/8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70/events?current_page=1&page_size=50
  ```

- **响应示例**：

  ```json
  {
      "run_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70",
      "events": [
          {
              "type": "event",
              "event_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70:1",
              "seq": 1,
              "method": "lifecycle",
              "params": {
                  "namespace": [],
                  "timestamp": 1714053834000,
                  "node": null,
                  "data": {
                      "event": "running",
                      "status": "running",
                      "phase": "preparing",
                      "message_id": "c0759ca8-2d35-4480-83a8-1f41f29d1401",
                      "input_message_id": "local-1714053834"
                  },
                  "metadata": {
                      "my_dify": {
                          "source": "workbench"
                      }
                  }
              }
          }
      ],
      "next_page": null
  }
  ```

---

## 03. 内部 Agent Protocol 模块

本模块用于平台内部已发布 Agent 之间通过 LangGraph SDK 互调，**路由注册在 `my_dify` 蓝图但没有任何 `@login_required`**，唯一凭证是 HMAC 签名的内部 Token。

调用方需要在 `headers` 中携带内部凭证，支持两种形式：自定义请求头 `X-MyDify-Internal-Token: <token>`，或标准形式 `Authorization: Bearer <token>`。

凭证结构为 `<base64url(payload)>.<signature>`，载荷包含 `v(版本，固定为 1)`、`account_id`、`parent_app_id`、`target_app_id` 和 `exp(过期时间戳)`。签发时有效期取 `max(60, AGENT_INTERNAL_PROTOCOL_TOKEN_TTL_SECONDS)`，默认 86400 秒。

**本模块与其余接口不同，使用真实 HTTP 状态码，响应体为 `{"detail": "..."}`**：

| 状态码 | 触发条件 |
|---|---|
| `200` | 查询类接口成功 |
| `202` | `POST:/agent-protocol/threads/:conversation_id/runs` 创建成功 |
| `204` | `POST:/agent-protocol/threads/:conversation_id/runs/:run_id/cancel` 成功（空响应体） |
| `401` | 凭证缺失、签名错误或已过期 |
| `403` | 账号或应用范围不符，或 `assistant_id` 不在授权范围 |
| `404` | 会话或运行不存在，或不属于该应用 |
| `409` | 创建运行时被业务冲突拒绝 |
| `422` | 请求体格式错误 |
| `500` | 未归类的业务异常 |

### 3.1 创建线程

- **接口说明**：该接口用于创建一个 SDK 线程占位。平台把 SDK 的 thread 映射为业务会话，创建出的会话名称为 `新的会话`，调用来源为 `creator_task`。

- **接口信息**：`内部凭证`+`POST:/agent-protocol/threads`

- **接口参数**：

  - 请求参数：无。
  - 响应参数：
    - `thread_id -> uuid`：线程 id，等于业务会话 id。
    - `created_at -> str`：创建时间，ISO 8601 UTC 字符串。
    - `updated_at -> str`：更新时间，ISO 8601 UTC 字符串。
    - `metadata -> dict`：线程元数据，包含 `graph_id`，取值为 `mydify:published:<target_app_id>`。
    - `status -> str`：线程状态，固定为 `idle`。
    - `values -> dict`：线程状态值，创建时为空对象。
    - `interrupts -> dict`：中断信息，创建时为空对象。

- **请求示例**：

  ```bash
  POST:/agent-protocol/threads
  X-MyDify-Internal-Token: eyJ2IjoxLCJhY2NvdW50X2lkIjoiYzkwOWNhZGItYjJlZS00ZmU2LTg4YjItYjJlOTFlYWIxYTdiIn0.3f2a1c9d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c
  ```

- **响应示例**：

  ```json
  {
      "thread_id": "2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8",
      "created_at": "2026-09-14T11:23:54Z",
      "updated_at": "2026-09-14T11:23:54Z",
      "metadata": {
          "graph_id": "mydify:published:5e7834dc-bbca-4ee5-9591-8f297f5acded"
      },
      "status": "idle",
      "values": {},
      "interrupts": {}
  }
  ```

### 3.2 获取线程状态

- **接口说明**：该接口用于异步子 Agent 轮询最终结果。**只返回最新一轮消息**（内部按 `page_size=1` 读取），更早的历史不通过该接口暴露。注意 `status` 对等待表单的运行会报 `idle`，因为线程忙碌判定只看 `running`；同一个运行通过 3.4 查询时会报 `running`。

- **接口信息**：`内部凭证`+`GET:/agent-protocol/threads/:conversation_id`

- **接口参数**：

  - 请求参数：
    - `conversation_id -> uuid`：路由参数，线程（会话）id，类型为 uuid。
  - 响应参数：
    - `thread_id -> uuid`：线程 id，等于业务会话 id。
    - `created_at -> str`：创建时间，ISO 8601 UTC 字符串。
    - `updated_at -> str`：更新时间，ISO 8601 UTC 字符串。
    - `metadata -> dict`：线程元数据，包含 `graph_id`。
    - `status -> str`：线程状态，取值为 `busy(运行中)` 或 `idle(空闲)`。
    - `values -> dict`：线程状态值，包含 `messages` 列表。
      - `type -> str`：消息角色，取值为 `human` 或 `ai`。
      - `id -> str`：消息 id，格式为 `<message_id>:human` 或 `<message_id>:assistant`。
      - `content -> str`：消息正文。
    - `interrupts -> dict`：中断信息，**当前实现始终为空对象**。

- **请求示例**：

  ```bash
  GET:/agent-protocol/threads/2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8
  X-MyDify-Internal-Token: eyJ2IjoxLCJhY2NvdW50X2lkIjoiYzkwOWNhZGItYjJlZS00ZmU2LTg4YjItYjJlOTFlYWIxYTdiIn0.3f2a1c9d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c
  ```

- **响应示例**：

  ```json
  {
      "thread_id": "2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8",
      "created_at": "2026-09-14T11:23:54Z",
      "updated_at": "2026-09-14T11:24:12Z",
      "metadata": {
          "graph_id": "mydify:published:5e7834dc-bbca-4ee5-9591-8f297f5acded"
      },
      "status": "idle",
      "values": {
          "messages": [
              {
                  "type": "human",
                  "id": "c0759ca8-2d35-4480-83a8-1f41f29d1401:human",
                  "content": "帮我汇总本周的项目进展"
              },
              {
                  "type": "ai",
                  "id": "c0759ca8-2d35-4480-83a8-1f41f29d1401:assistant",
                  "content": "本周共完成 3 项任务。"
              }
          ]
      },
      "interrupts": {}
  }
  ```

### 3.3 创建后台运行

- **接口说明**：该接口用于创建一个后台运行，并立即返回运行摘要，不阻塞等待执行完成。请求必须携带与 `target_app_id` 匹配的 `assistant_id`。**该入口固定以 `user_input_enabled=false` 启动**，因此通过内部 SDK 调用的 Agent 不会向用户索要填写表单。

- **接口信息**：`内部凭证`+`POST:/agent-protocol/threads/:conversation_id/runs`

- **接口参数**：

  - 请求参数：
    - `conversation_id -> uuid`：路由参数，线程（会话）id，类型为 uuid。
    - `assistant_id -> str`：必填参数，图标识，必须等于 `mydify:published:<target_app_id>`，否则返回 403。
    - `input -> dict`：可选参数，运行输入，结构同工作台 `run.start` 的输入对象（`messages` 或 `query`）。
    - `command -> dict`：可选参数，运行命令；只识别 `command.update` 中的对象作为输入。
    - `multitask_strategy -> str`：可选参数，多任务策略，枚举值为 `reject(默认，冲突时返回 409)` 或 `interrupt(先停止前序运行再启动)`。
    - `input` 与 `command` **必须且只能提供一个**，否则返回 422 `Run 必须且只能提供 input 或 command`。
  - 响应参数（成功，HTTP 202）：
    - `run_id -> uuid`：运行 id，类型为 uuid。
    - `thread_id -> uuid`：线程 id，值就是会话 id。
    - `assistant_id -> str`：图标识，格式为 `mydify:published:<app_id>`。
    - `status -> str`：运行状态，取值为 `pending`、`running`、`success`、`error`、`cancelled`。平台内部状态映射规则为：`running` 和 `waiting_input` → `running`，`completed` → `success`，`failed` → `error`，`cancelled` → `cancelled`。
    - `multitask_strategy -> str`：本次使用的多任务策略，请求未传时为 `reject`。
    - `metadata -> dict`：运行元数据，当前固定为空对象。
    - `created_at -> str`：创建时间，ISO 8601 UTC 字符串。
    - `updated_at -> str`：更新时间，ISO 8601 UTC 字符串。
    - `error -> str | null`：失败原因，仅 `status` 为 `error` 时返回。
  - 响应参数（冲突，HTTP 409）：
    - `detail -> str`：冲突原因，例如前序任务尚未停止。

- **请求示例**：

  ```bash
  POST:/agent-protocol/threads/2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8/runs
  X-MyDify-Internal-Token: eyJ2IjoxLCJhY2NvdW50X2lkIjoiYzkwOWNhZGItYjJlZS00ZmU2LTg4YjItYjJlOTFlYWIxYTdiIn0.3f2a1c9d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c
  
  {
      "assistant_id": "mydify:published:5e7834dc-bbca-4ee5-9591-8f297f5acded",
      "input": {
          "messages": [
              {
                  "type": "human",
                  "content": "帮我汇总本周的项目进展"
              }
          ]
      },
      "multitask_strategy": "reject"
  }
  ```

- **响应示例**：

  ```json
  {
      "run_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70",
      "thread_id": "2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8",
      "assistant_id": "mydify:published:5e7834dc-bbca-4ee5-9591-8f297f5acded",
      "status": "running",
      "multitask_strategy": "reject",
      "metadata": {},
      "created_at": "2026-09-14T11:23:54Z",
      "updated_at": "2026-09-14T11:23:54Z",
      "error": null
  }
  ```

### 3.4 获取指定运行

- **接口说明**：该接口用于查询后台运行的最新状态，是异步子 Agent 获取最终结果的主要方式。查询前会先确认该运行确实属于当前线程与授权应用范围，避免跨应用越权。

- **接口信息**：`内部凭证`+`GET:/agent-protocol/threads/:conversation_id/runs/:run_id`

- **接口参数**：

  - 请求参数：
    - `conversation_id -> uuid`：路由参数，线程（会话）id，类型为 uuid。
    - `run_id -> uuid`：路由参数，运行 id，类型为 uuid。
  - 响应参数：
    - `run_id -> uuid`：运行 id，类型为 uuid。
    - `thread_id -> uuid`：线程 id，等于会话 id。
    - `assistant_id -> str`：图标识，按运行所属应用生成。
    - `status -> str`：运行状态，取值与 3.3 一致。
    - `multitask_strategy -> str`：多任务策略，查询结果固定为 `reject`。
    - `metadata -> dict`：运行元数据，当前固定为空对象。
    - `created_at -> str`：创建时间，ISO 8601 UTC 字符串。
    - `updated_at -> str`：更新时间，ISO 8601 UTC 字符串。
    - `error -> str | null`：失败原因；状态为 `error` 且平台未记录原因时返回 `Agent 运行失败`。

- **请求示例**：

  ```bash
  GET:/agent-protocol/threads/2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8/runs/8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70
  X-MyDify-Internal-Token: eyJ2IjoxLCJhY2NvdW50X2lkIjoiYzkwOWNhZGItYjJlZS00ZmU2LTg4YjItYjJlOTFlYWIxYTdiIn0.3f2a1c9d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c
  ```

- **响应示例**：

  ```json
  {
      "run_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70",
      "thread_id": "2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8",
      "assistant_id": "mydify:published:5e7834dc-bbca-4ee5-9591-8f297f5acded",
      "status": "success",
      "multitask_strategy": "reject",
      "metadata": {},
      "created_at": "2026-09-14T11:23:54Z",
      "updated_at": "2026-09-14T11:24:12Z",
      "error": null
  }
  ```

### 3.5 取消指定运行

- **接口说明**：该接口用于取消一个后台运行，底层复用工作台的指定运行取消逻辑。**成功时返回空的 204 响应**，不返回任何响应体。

- **接口信息**：`内部凭证`+`POST:/agent-protocol/threads/:conversation_id/runs/:run_id/cancel`

- **接口参数**：

  - 请求参数：
    - `conversation_id -> uuid`：路由参数，线程（会话）id，类型为 uuid。
    - `run_id -> uuid`：路由参数，需要取消的运行 id，类型为 uuid。
  - 响应参数：无（HTTP 204 空响应体）。

- **请求示例**：

  ```bash
  POST:/agent-protocol/threads/2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8/runs/8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70/cancel
  X-MyDify-Internal-Token: eyJ2IjoxLCJhY2NvdW50X2lkIjoiYzkwOWNhZGItYjJlZS00ZmU2LTg4YjItYjJlOTFlYWIxYTdiIn0.3f2a1c9d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c
  ```

- **响应示例**：

  ```
  HTTP/1.1 204 NO CONTENT
  ```

---

## 04. Creator 任务中心模块

### 4.1 创建任务会话

- **接口说明**：该接口用于在任务中心为指定应用创建一个新会话。应用必须已发布且属于当前账号，否则返回 `应用不存在、未发布或无权访问`。

- **接口信息**：`授权`+`POST:/tasks/apps/:app_id/conversations`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
  - 响应参数：
    - `id -> uuid`：会话 id，类型为 uuid。

- **请求示例**：

  ```bash
  POST:/tasks/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/conversations
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "id": "2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8"
      },
      "message": ""
  }
  ```

### 4.2 获取任务 Agent 列表

- **接口说明**：该接口用于获取当前账号下所有已发布 Agent 在任务中心的汇总信息。没有会话的 Agent 也会返回，统计值按 0 处理。

- **接口信息**：`授权`+`GET:/tasks/agents`

- **接口参数**：

  - 请求参数：无。
  - 响应参数：
    - `list -> list[dict]`：Agent 列表，类型为字典列表。
      - `id -> str`：应用 id，类型为字符串形式的 uuid。
      - `name -> str`：应用名称。
      - `icon -> str`：应用图标。
      - `description -> str`：应用描述。
      - `status -> str`：应用状态，任务中心只返回 `published`。
      - `conversation_count -> int`：该应用在任务中心的会话数量。
      - `last_active_at -> int`：最近活跃时间，类型为时间戳，单位为秒；没有会话时为 0。

- **请求示例**：

  ```bash
  GET:/tasks/agents
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "list": [
              {
                  "id": "5e7834dc-bbca-4ee5-9591-8f297f5acded",
                  "name": "项目周报助手",
                  "icon": "https://cdn.example.com/icon.png",
                  "description": "根据附件整理项目周报",
                  "status": "published",
                  "conversation_count": 3,
                  "last_active_at": 1714053834
              }
          ]
      },
      "message": ""
  }
  ```

### 4.3 获取指定任务 Agent 详情

- **接口说明**：该接口用于获取任务中心运行所需的已发布 Agent 完整运行配置。**出于安全考虑不返回 WebApp token**。

- **接口信息**：`授权`+`GET:/tasks/agents/:app_id`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
  - 响应参数：
    - `id -> str`：应用 id，类型为字符串形式的 uuid。
    - `name -> str`：应用名称。
    - `icon -> str`：应用图标。
    - `description -> str`：应用描述。
    - `app_config -> dict`：应用的运行时配置对象，字段与草稿配置一致，详见 5.7。

- **请求示例**：

  ```bash
  GET:/tasks/agents/5e7834dc-bbca-4ee5-9591-8f297f5acded
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "id": "5e7834dc-bbca-4ee5-9591-8f297f5acded",
          "name": "项目周报助手",
          "icon": "https://cdn.example.com/icon.png",
          "description": "根据附件整理项目周报",
          "app_config": {
              "id": "1550b71a-1444-47ed-a59d-c2f080fbae94",
              "preset_prompt": "你是一名项目经理。",
              "opening_statement": "",
              "opening_questions": [],
              "user_input": {
                  "enable": true
              }
          }
      },
      "message": ""
  }
  ```

### 4.4 获取任务会话分页列表

- **接口说明**：该接口用于获取任务中心的统一会话历史，**包含本账号下的 Creator 任务会话与 WebApp 会话**，支持按应用过滤和关键词搜索。接口使用**游标分页**（不是页码分页），按 `updated_at` 倒序、`id` 倒序稳定排序，响应结构为 `{list, page}`。

- **接口信息**：`授权`+`GET:/tasks/conversations`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：可选参数，按应用过滤，类型为 uuid。
    - `search -> str`：可选参数，搜索词，长度不超过 255，在后端对会话名称和摘要做模糊匹配。
    - `before -> uuid`：可选参数，游标，传入上一页返回的 `page.next_before`；必须是合法 UUID，否则返回 `before必须是合法的UUID`。
    - `page_size -> int`：可选参数，每页数据条数，默认为 20，范围为 1–50。
  - 响应参数：
    - `list -> list[dict]`：会话列表，类型为字典列表。
      - `id -> str`：会话 id，类型为字符串形式的 uuid。
      - `name -> str`：会话名称，名称为空时返回 `新的会话`。
      - `summary -> str`：会话摘要，为空时返回空字符串。
      - `is_pinned -> bool`：是否置顶。
      - `updated_at -> int`：更新时间，类型为时间戳，单位为秒。
      - `created_at -> int`：创建时间，类型为时间戳，单位为秒。
      - `last_run_status -> str`：该会话最新运行的状态，取值为 `running`、`waiting_input`、`completed`、`failed`、`cancelled` 或 `idle(没有运行记录)`。
      - `agent -> dict`：会话所属应用信息，包含 `id`、`name`、`icon` 和 `description`。
    - `page -> dict`：游标分页信息。
      - `has_more -> bool`：是否还有下一页。
      - `next_before -> str | null`：下一页游标，取当前页最后一条会话的 id；没有下一页时为 `null`。

- **请求示例**：

  ```bash
  GET:/tasks/conversations?page_size=20&app_id=5e7834dc-bbca-4ee5-9591-8f297f5acded&search=周报
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "list": [
              {
                  "id": "2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8",
                  "name": "根据附件整理项目周报",
                  "summary": "整理了本周的项目进展",
                  "is_pinned": false,
                  "updated_at": 1714053834,
                  "created_at": 1714053834,
                  "last_run_status": "completed",
                  "agent": {
                      "id": "5e7834dc-bbca-4ee5-9591-8f297f5acded",
                      "name": "项目周报助手",
                      "icon": "https://cdn.example.com/icon.png",
                      "description": "根据附件整理项目周报"
                  }
              }
          ],
          "page": {
              "has_more": false,
              "next_before": null
          }
      },
      "message": ""
  }
  ```

### 4.5 获取指定任务会话

- **接口说明**：该接口用于读取单个任务会话的摘要信息，权限范围与列表接口一致，不能通过该接口绕过历史归属兼容规则访问他人的会话。

- **接口信息**：`授权`+`GET:/tasks/conversations/:conversation_id`

- **接口参数**：

  - 请求参数：
    - `conversation_id -> uuid`：路由参数，会话 id，类型为 uuid。
  - 响应参数：与 4.4 中 `list` 的单个元素结构完全一致。

- **请求示例**：

  ```bash
  GET:/tasks/conversations/2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "id": "2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8",
          "name": "根据附件整理项目周报",
          "summary": "整理了本周的项目进展",
          "is_pinned": false,
          "updated_at": 1714053834,
          "created_at": 1714053834,
          "last_run_status": "completed",
          "agent": {
              "id": "5e7834dc-bbca-4ee5-9591-8f297f5acded",
              "name": "项目周报助手",
              "icon": "https://cdn.example.com/icon.png",
              "description": "根据附件整理项目周报"
          }
      },
      "message": ""
  }
  ```

### 4.6 更新任务会话名称

- **接口说明**：该接口用于修改任务会话的名称，成功后返回更新后的会话摘要。名称会做去除首尾空白处理。

- **接口信息**：`授权`+`PATCH:/tasks/conversations/:conversation_id/name`

- **接口参数**：

  - 请求参数：
    - `conversation_id -> uuid`：路由参数，会话 id，类型为 uuid。
    - `name -> str`：必填参数，会话名称，长度不超过 255，不能为空。
  - 响应参数：与 4.4 中 `list` 的单个元素结构完全一致，`name` 为更新后的值。

- **请求示例**：

  ```bash
  PATCH:/tasks/conversations/2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8/name
  
  {
      "name": "本周项目周报"
  }
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "id": "2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8",
          "name": "本周项目周报",
          "summary": "整理了本周的项目进展",
          "is_pinned": false,
          "updated_at": 1714053900,
          "created_at": 1714053834,
          "last_run_status": "completed",
          "agent": {
              "id": "5e7834dc-bbca-4ee5-9591-8f297f5acded",
              "name": "项目周报助手",
              "icon": "https://cdn.example.com/icon.png",
              "description": "根据附件整理项目周报"
          }
      },
      "message": ""
  }
  ```

### 4.7 更新任务会话置顶状态

- **接口说明**：该接口用于设置或取消任务会话的置顶状态。

- **接口信息**：`授权`+`PATCH:/tasks/conversations/:conversation_id/is-pinned`

- **接口参数**：

  - 请求参数：
    - `conversation_id -> uuid`：路由参数，会话 id，类型为 uuid。
    - `is_pinned -> bool`：必填参数，是否置顶，必须是布尔值，否则返回 `is_pinned必须是布尔值`。
  - 响应参数：与 4.4 中 `list` 的单个元素结构完全一致，`is_pinned` 为更新后的值。

- **请求示例**：

  ```bash
  PATCH:/tasks/conversations/2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8/is-pinned
  
  {
      "is_pinned": true
  }
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "id": "2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8",
          "name": "本周项目周报",
          "summary": "整理了本周的项目进展",
          "is_pinned": true,
          "updated_at": 1714053900,
          "created_at": 1714053834,
          "last_run_status": "completed",
          "agent": {
              "id": "5e7834dc-bbca-4ee5-9591-8f297f5acded",
              "name": "项目周报助手",
              "icon": "https://cdn.example.com/icon.png",
              "description": "根据附件整理项目周报"
          }
      },
      "message": ""
  }
  ```

### 4.8 删除任务会话

- **接口说明**：该接口用于删除指定任务会话。删除前会先清理该会话可恢复的运行资源，**清理失败时不会提交删除标记**，会话仍然保留。

- **接口信息**：`授权`+`DELETE:/tasks/conversations/:conversation_id`

- **接口参数**：

  - 请求参数：
    - `conversation_id -> uuid`：路由参数，会话 id，类型为 uuid。
  - 响应参数：无数据，`data` 为空对象。

- **请求示例**：

  ```bash
  DELETE:/tasks/conversations/2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {},
      "message": "删除会话成功"
  }
  ```

---

## 05. 应用配置模块

### 5.1 获取应用基础信息

- **接口说明**：传递对应的应用 id，获取当前应用的基础信息，同时返回该应用**调试工作台使用的会话 id**。

- **接口信息**：`授权`+`GET:/apps/:app_id`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，必填，需要获取的应用 id。
  - 响应参数：
    - `id -> uuid`：应用 id，类型为 uuid。
    - `conversation_id -> uuid`：调试会话 id，类型为 uuid，如果没有则为空。
    - `name -> string`：应用名称。
    - `icon -> string`：应用图标。
    - `description -> string`：应用描述。
    - `status -> string`：应用的状态，支持 `published(已发布)` 和 `draft(草稿)`。
    - `draft_updated_at -> int`：应用草稿的更新时间，类型为时间戳，单位为秒。
    - `updated_at -> int`：应用的更新时间，类型为时间戳，单位为秒。
    - `created_at -> int`：应用的创建时间，类型为时间戳，单位为秒。

- **请求示例**：

  ```bash
  GET:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "id": "5e7834dc-bbca-4ee5-9591-8f297f5acded",
          "conversation_id": "46db30d1-3199-4e79-a0cd-abf12fa6858f",
          "name": "项目周报助手",
          "icon": "https://cdn.example.com/icon.png",
          "description": "根据附件整理项目周报",
          "status": "published",
          "draft_updated_at": 1714053834,
          "updated_at": 1714053834,
          "created_at": 1714053834
      },
      "message": ""
  }
  ```

### 5.2 在个人空间下新增应用

- **接口说明**：该接口用于在个人空间下新增 Agent 应用，创建的 Agent 应用会添加一个默认的草稿配置信息。

- **接口信息**：`授权`+`POST:/apps`

- **接口参数**：

  - 请求参数：
    - `name -> str`：Agent 应用的名称，类型为字符串，必填，长度不超过 40 个字符。
    - `icon -> str`：可选参数，Agent 应用的图标 URL 地址，必须是合法的 URL 链接。
    - `description -> str`：可选参数，Agent 应用的描述信息，类型为字符串，长度不超过 800 个字符。
  - 响应参数：
    - `id -> uuid`：创建的 Agent 应用 id，类型为 uuid。

- **请求示例**：

  ```bash
  POST:/apps
  
  {
      "name": "LLM应用产品经理",
      "icon": "https://cdn.example.com/2024/05/14/218e5217-ab10-4634-9681-022867955f1b.png",
      "description": "一个能帮你解答关于LLM应用产品开发的Agent智能体。"
  }
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "id": "1550b71a-1444-47ed-a59d-c2f080fbae94"
      },
      "message": ""
  }
  ```

### 5.3 修改指定的应用

- **接口说明**：该接口用于修改指定应用的基础信息，字段校验规则与创建接口一致。

- **接口信息**：`授权`+`POST:/apps/:app_id`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，需要修改的应用 id。
    - `name -> str`：Agent 应用的名称，类型为字符串，必填，长度不超过 40 个字符。
    - `icon -> str`：可选参数，Agent 应用的图标 URL 地址，必须是合法的 URL 链接。
    - `description -> str`：可选参数，Agent 应用的描述信息，长度不超过 800 个字符。
  - 响应参数：无数据，`data` 为空对象。

- **请求示例**：

  ```bash
  POST:/apps/1550b71a-1444-47ed-a59d-c2f080fbae94
  
  {
      "name": "LLM应用产品经理",
      "description": "一个能帮你解答关于LLM应用产品开发的Agent智能体。"
  }
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {},
      "message": "修改Agent智能体应用成功"
  }
  ```

### 5.4 删除指定的应用

- **接口说明**：该接口用于删除指定的 Agent 智能体应用，删除之后，应用无法进行复原与调试，也无法使用开放 API 进行调试、用户无法访问该应用产生的所有会话信息等内容。删除应用会同时清空该应用下所有主体的长期记忆。

- **接口信息**：`授权`+`POST:/apps/:app_id/delete`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：需要删除的应用 id，类型为 uuid。
  - 响应参数：无数据，`data` 为空对象。

- **请求示例**：

  ```bash
  POST:/apps/1550b71a-1444-47ed-a59d-c2f080fbae94/delete
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {},
      "message": "删除Agent智能体应用成功"
  }
  ```

### 5.5 复制指定的应用

- **接口说明**：该接口用于快速拷贝指定应用为一个新应用，新应用归属当前登录账号。

- **接口信息**：`授权`+`POST:/apps/:app_id/copy`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，需要拷贝的应用 id，类型为 uuid。
  - 响应参数：
    - `id -> uuid`：新应用的 id，类型为 uuid。

- **请求示例**：

  ```bash
  POST:/apps/1550b71a-1444-47ed-a59d-c2f080fbae94/copy
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "id": "9a1f2b3c-4d5e-4f60-9a71-2b3c4d5e6f70"
      },
      "message": ""
  }
  ```

### 5.6 获取应用分页列表

- **接口说明**：该接口用于获取当前登录账号下创建的应用分页列表数据，该接口支持分页+搜索+状态过滤。列表中的模型信息只投影 `provider`、`model` 和 `provider_connection_id` 三个字段。

- **接口信息**：`授权`+`GET:/apps`

- **接口参数**：

  - 请求参数：
    - `current_page -> int`：可选参数，当前页数，默认为 1，范围为 1–9999。
    - `page_size -> int`：可选参数，每页数据条数，默认为 20，范围为 1–50。
    - `search_word -> str`：可选参数，搜索词，在后端会使用应用的名称进行模糊匹配。
    - `status -> str`：可选参数，应用状态过滤，仅支持 `draft` 或 `published`，否则返回 `应用状态仅支持 draft 或 published`。
  - 响应参数：
    - `list -> list[dict]`：分页后的列表数据，类型为字典列表。
      - `id -> uuid`：Agent 智能体应用的 id，类型为 uuid。
      - `name -> string`：应用名称。
      - `icon -> string`：应用图标。
      - `description -> string`：应用描述。
      - `preset_prompt -> string`：应用的人设提示词。
      - `model_config -> dict`：模型配置，只包含 `provider`、`model`、`provider_connection_id`。
      - `status -> string`：应用的状态，支持 `published(已发布)` 和 `draft(草稿)`。
      - `updated_at -> int`：应用的更新时间，类型为时间戳，单位为秒。
      - `created_at -> int`：应用的创建时间，类型为时间戳，单位为秒。
    - `paginator -> dict`：分页字典信息。
      - `current_page -> int`：当前的页数。
      - `page_size -> int`：每页的条数。
      - `total_page -> int`：总页数。
      - `total_record -> int`：总记录条数。

  已发布应用读取发布配置，未发布应用读取草稿配置。

- **请求示例**：

  ```bash
  GET:/apps?current_page=1&page_size=20&status=published&search_word=周报
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "list": [
              {
                  "id": "5e7834dc-bbca-4ee5-9591-8f297f5acded",
                  "name": "项目周报助手",
                  "icon": "https://cdn.example.com/icon.png",
                  "description": "根据附件整理项目周报",
                  "preset_prompt": "你是一名项目经理。",
                  "model_config": {
                      "provider": "openai",
                      "model": "gpt-4o-mini",
                      "provider_connection_id": "1550b71a-1444-47ed-a59d-c2f080fbae94"
                  },
                  "status": "published",
                  "updated_at": 1714053834,
                  "created_at": 1714053834
              }
          ],
          "paginator": {
              "current_page": 1,
              "page_size": 20,
              "total_page": 1,
              "total_record": 1
          }
      },
      "message": ""
  }
  ```

### 5.7 获取应用的草稿配置

- **接口说明**：该接口用于获取应用的最新草稿配置。读取时会顺带清理已失效的关联数据：无效的模型配置回退到默认值，已删除的工具、知识库和工作流会从配置中移除并保存。

- **接口信息**：`授权`+`GET:/apps/:app_id/draft-app-config`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
  - 响应参数：
    - `id -> str`：配置记录 id，类型为字符串形式的 uuid。
    - `model_config -> dict`：模型引用与参数，包含 `provider`、`model`、`parameters`，可选包含 `provider_connection_id`、`features`、`modalities`、`input_modalities`、`output_modalities`、`native_file_input`。
    - `preset_prompt -> str`：人设提示词，长度不超过 2000。
    - `skills -> list[dict]`：Skill 绑定列表，元素结构为 `{name, version, digest, enabled}`。
    - `subagents -> list[dict]`：子 Agent 配置列表，字段随 `mode` 与 `source` 条件变化，详见下方说明。
    - `tools -> list[dict]`：工具绑定列表，最多 5 个，元素包含 `type`、`provider_id`、`tool_id` 和 `params`。
    - `workflows -> list[dict]`：工作流引用列表，最多 5 个，元素包含 `id`。
    - `datasets -> list[dict]`：知识库引用列表，最多 5 个，元素包含 `id`。
    - `retrieval_config -> dict`：检索参数，包含 `retrieval_strategy`、`k` 和 `score`。
    - `long_term_memory -> dict`：长期记忆开关，包含 `enable`。
    - `opening_statement -> str`：开场白，长度不超过 2000。
    - `opening_questions -> list[str]`：开场问题，最多 3 条。
    - `user_input -> dict`：用户提问表单开关，包含 `enable`。
    - `speech_to_text -> dict`：语音转文字开关，包含 `enable`。
    - `text_to_speech -> dict`：文字转语音配置，包含 `enable`、`voice` 和 `auto_play`。
    - `suggested_after_answer -> dict`：回答后建议问题开关，包含 `enable`。
    - `review_config -> dict`：审核配置，包含 `enable`、`keywords`、`inputs_config` 和 `outputs_config`。
    - `updated_at -> int`：配置更新时间，类型为时间戳，单位为秒。
    - `created_at -> int`：配置创建时间，类型为时间戳，单位为秒。

  **`subagents` 元素的字段不是固定集合**，由服务端规范化后写入，公共字段为 `name`、`description`、`mode`、`context_passing`、`tools` 和 `skills`：

  - `mode = "async"`：额外包含 `source`、`graph_id`、`headers`；`source` 为 `published` 时包含 `published_app_id`；配置了 `url` 时包含 `url`。
  - `mode = "sync"` 且 `source = "published"`：额外包含 `source` 和 `published_app_id`。
  - `mode = "sync"` 且 `source = "custom"`：额外包含 `system_prompt`，以及 `model_mode` 和对应的 `model`、`model_provider`、`model_connection_id`。

  异步已发布子 Agent 的 `graph_id` 格式为 `mydify:published:<published_app_id>`。

- **请求示例**：

  ```bash
  GET:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/draft-app-config
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "id": "1550b71a-1444-47ed-a59d-c2f080fbae94",
          "model_config": {
              "provider": "openai",
              "model": "gpt-4o-mini",
              "parameters": {
                  "temperature": 0.5,
                  "top_p": 0.85,
                  "frequency_penalty": 0.2,
                  "presence_penalty": 0.2,
                  "max_tokens": 8192
              },
              "provider_connection_id": "c0759ca8-2d35-4480-83a8-1f41f29d1401"
          },
          "preset_prompt": "你是一名项目经理。",
          "skills": [
              {
                  "name": "office-docs",
                  "version": "1.0.0",
                  "digest": "3f2a1c9d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c",
                  "enabled": true
              }
          ],
          "subagents": [],
          "tools": [],
          "workflows": [],
          "datasets": [],
          "retrieval_config": {
              "retrieval_strategy": "semantic",
              "k": 10,
              "score": 0.5
          },
          "long_term_memory": {
              "enable": false
          },
          "opening_statement": "",
          "opening_questions": [],
          "user_input": {
              "enable": true
          },
          "speech_to_text": {
              "enable": false
          },
          "text_to_speech": {
              "enable": false,
              "voice": "xiaoxiao",
              "auto_play": false
          },
          "suggested_after_answer": {
              "enable": true
          },
          "review_config": {
              "enable": false,
              "keywords": [],
              "inputs_config": {
                  "enable": false,
                  "preset_response": ""
              },
              "outputs_config": {
                  "enable": false
              }
          },
          "updated_at": 1714053834,
          "created_at": 1714053834
      },
      "message": ""
  }
  ```

### 5.8 更新应用的草稿配置

- **接口说明**：该接口用于部分更新应用的草稿配置，只提交需要修改的字段即可。**请求体为 JSON，且不接受任何显式 `null` 值**：`null` 会被明确拒绝而不是当作"不更新"处理；同时补丁不能为空对象。`model_config` 传空对象 `{}` 时会被规范化为"不更新"而不是校验失败。

- **接口信息**：`授权`+`POST:/apps/:app_id/draft-app-config`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `model_config -> dict`：可选参数，模型引用与参数，包含 `provider`、`model`、`parameters`，可选包含 `provider_connection_id`、`features`、`modalities`、`input_modalities`、`output_modalities`、`native_file_input`。
    - `preset_prompt -> str`：可选参数，人设提示词，长度不超过 2000。
    - `skills -> list[dict]`：可选参数，Skill 绑定列表，元素结构为 `{name, version, digest, enabled}`；`name` 需匹配 `^[a-z][a-z0-9-]*$`，长度 1–64，列表内不能重名。
    - `subagents -> list[dict]`：可选参数，子 Agent 配置列表，结构见 5.7。
    - `tools -> list[dict]`：可选参数，工具绑定列表，最多 5 个且不能重复，元素包含 `type`、`provider_id`、`tool_id` 和 `params`；`type` 枚举为 `builtin_tool`、`api_tool`、`mcp_tool`。
    - `workflows -> list[uuid]`：可选参数，工作流 id 列表，最多 5 个且不能重复。
    - `datasets -> list[uuid]`：可选参数，知识库 id 列表，最多 5 个且不能重复。
    - `retrieval_config -> dict`：可选参数，检索参数，`retrieval_strategy` 枚举为 `semantic`、`full_text`、`hybrid`，`k` 范围 0–10，`score` 范围 0–1。
    - `long_term_memory -> dict`：可选参数，长期记忆开关。
    - `opening_statement -> str`：可选参数，开场白，长度不超过 2000。
    - `opening_questions -> list[str]`：可选参数，开场问题，最多 3 条。
    - `user_input -> dict`：可选参数，用户提问表单开关。
    - `speech_to_text -> dict`：可选参数，语音转文字开关。
    - `text_to_speech -> dict`：可选参数，文字转语音配置，`voice` 必须是受支持的音色。
    - `suggested_after_answer -> dict`：可选参数，回答后建议问题开关。
    - `review_config -> dict`：可选参数，审核配置；启用时要求 `keywords` 非空、输入审核与输出审核至少开启一项，且开启输入审核时预设响应不能为空。
  - 响应参数：无数据，`data` 为空对象。

  常见校验失败文案：`草稿配置格式错误: <详情>`、`配置字段不能为 null: <字段名列表>`、`草稿配置不能为空`、`Agent 绑定的工具数不能超过 5`、`绑定插件存在重复`、`Agent 绑定的工作流数量不能超过 5 个`、`绑定工作流存在重复`、`Agent 绑定的知识库数量不能超过 5 个`、`绑定知识库存在重复`、`Skill 不存在或未内置: <name>`、`Skill 版本不匹配: <name>`、`不支持的文本转语音音色`。

- **请求示例**：

  ```bash
  POST:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/draft-app-config
  
  {
      "preset_prompt": "你是一名项目经理，负责整理项目周报。",
      "user_input": {
          "enable": true
      },
      "long_term_memory": {
          "enable": true
      }
  }
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {},
      "message": "更新应用草稿配置成功"
  }
  ```

### 5.9 获取可引用的已发布 Agent 列表

- **接口说明**：该接口用于获取当前应用可引用的同账号已发布 Agent 候选列表，用于编排子 Agent 时选择"引用已发布 Agent"。

- **接口信息**：`授权`+`GET:/apps/:app_id/published-subagents`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `search_word -> str`：可选参数，搜索词，在后端按应用名称模糊匹配。
  - 响应参数：
    - `list -> list[dict]`：候选 Agent 列表，类型为字典列表。
      - `id -> str`：应用 id，类型为字符串形式的 uuid。
      - `name -> str`：应用名称。
      - `icon -> str`：应用图标。
      - `description -> str`：应用描述。
      - `status -> str`：应用状态，固定为 `published`。
      - `model -> dict`：发布配置中的模型信息，只包含 `provider` 和 `model`。

- **请求示例**：

  ```bash
  GET:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/published-subagents?search_word=周报
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "list": [
              {
                  "id": "9a1f2b3c-4d5e-4f60-9a71-2b3c4d5e6f70",
                  "name": "数据汇总助手",
                  "icon": "https://cdn.example.com/icon2.png",
                  "description": "负责汇总多来源数据",
                  "status": "published",
                  "model": {
                      "provider": "openai",
                      "model": "gpt-4o-mini"
                  }
              }
          ]
      },
      "message": ""
  }
  ```

### 5.10 发布应用配置

- **接口说明**：该接口用于把当前应用的草稿配置发布（或更新发布）为运行时配置。发布前会校验已发布 Agent 引用：不能引用当前应用自身、不能存在循环引用、嵌套层级不能超过 32 层。

- **接口信息**：`授权`+`POST:/apps/:app_id/publish`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
  - 响应参数：无数据，`data` 为空对象。

- **请求示例**：

  ```bash
  POST:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/publish
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {},
      "message": "发布/更新应用配置成功"
  }
  ```

### 5.11 取消发布应用配置

- **接口说明**：该接口用于取消已发布的应用配置，应用回到草稿状态，发布配置引用与关联数据会被清理。**应用当前未发布时返回 `当前应用未发布，请核实后重试`**。

- **接口信息**：`授权`+`POST:/apps/:app_id/cancel-publish`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
  - 响应参数：无数据，`data` 为空对象。

- **请求示例**：

  ```bash
  POST:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/cancel-publish
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {},
      "message": "取消发布应用配置成功"
  }
  ```

### 5.12 获取应用发布历史列表

- **接口说明**：该接口用于获取应用的发布历史配置分页列表数据，只返回版本标识与创建时间。

- **接口信息**：`授权`+`GET:/apps/:app_id/publish-histories`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `current_page -> int`：可选参数，当前页数，默认为 1，范围为 1–9999。
    - `page_size -> int`：可选参数，每页数据条数，默认为 20，范围为 1–50。
  - 响应参数：
    - `list -> list[dict]`：分页后的发布历史列表，类型为字典列表。
      - `id -> uuid`：配置版本 id，类型为 uuid。
      - `version -> int`：版本号，类型为整型。
      - `created_at -> int`：发布时间，类型为时间戳，单位为秒。
    - `paginator -> dict`：分页字典信息，包含 `current_page`、`page_size`、`total_page`、`total_record`。

- **请求示例**：

  ```bash
  GET:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/publish-histories?current_page=1&page_size=20
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "list": [
              {
                  "id": "b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e",
                  "version": 1,
                  "created_at": 1714053834
              }
          ],
          "paginator": {
              "current_page": 1,
              "page_size": 20,
              "total_page": 1,
              "total_record": 1
          }
      },
      "message": ""
  }
  ```

### 5.13 回退历史配置至草稿

- **接口说明**：该接口用于把指定的历史发布版本回退到草稿中，回退后可以继续编辑并重新发布。

- **接口信息**：`授权`+`POST:/apps/:app_id/fallback-history`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `app_config_version_id -> str`：必填参数，需要回退的配置版本 id，必须是合法 UUID，否则返回 `回退配置版本id必须为UUID`。
  - 响应参数：无数据，`data` 为空对象。

- **请求示例**：

  ```bash
  POST:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/fallback-history
  
  {
      "app_config_version_id": "b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e"
  }
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {},
      "message": "回退历史配置至草稿成功"
  }
  ```

### 5.14 获取应用的发布配置信息

- **接口说明**：该接口用于获取应用的 WebApp 凭证与应用作为子 Agent 被引用时的信息。**该接口已由 Agent 工作台线程生命周期管理替代，属于保留的兼容接口。**

- **接口信息**：`授权`+`GET:/apps/:app_id/published-config`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
  - 响应参数：
    - `web_app -> dict`：WebApp 访问信息。
      - `token -> str`：WebApp 凭证标识；应用未发布时为空字符串。
      - `status -> str`：应用状态。
    - `subagent -> dict`：作为子 Agent 被引用时的信息。
      - `enabled -> bool`：是否可被引用，仅应用已发布时为 `true`。
      - `app_id -> str`：应用 id，类型为字符串形式的 uuid。
      - `name -> str`：应用名称。

- **请求示例**：

  ```bash
  GET:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/published-config
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "web_app": {
              "token": "1550b71asdfd94",
              "status": "published"
          },
          "subagent": {
              "enabled": true,
              "app_id": "5e7834dc-bbca-4ee5-9591-8f297f5acded",
              "name": "项目周报助手"
          }
      },
      "message": ""
  }
  ```

### 5.15 重新生成 WebApp 凭证标识

- **接口说明**：该接口用于重新生成应用的 WebApp 凭证标识，旧标识立即失效。**应用未发布时返回 `应用未发布，无法生成WebApp凭证标识`**。

- **接口信息**：`授权`+`POST:/apps/:app_id/published-config/regenerate-web-app-token`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
  - 响应参数：
    - `token -> str`：新生成的 WebApp 凭证标识。

- **请求示例**：

  ```bash
  POST:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/published-config/regenerate-web-app-token
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "token": "9f2c1a4b3d5e4f60"
      },
      "message": ""
  }
  ```

---

## 06. Skill 能力模块

### 6.1 获取内置 Skill 模板目录

- **接口说明**：该接口用于获取平台内置的 Skill 模板目录。目录来自服务端文件系统（默认 `resources/agent_skills/builtin`），**没有数据库表，也没有上传接口**。

- **接口信息**：`授权`+`GET:/skills`

- **接口参数**：

  - 请求参数：无。
  - 响应参数：
    - `items -> list[dict]`：Skill 目录列表，按名称升序。
      - `name -> str`：Skill 名称。
      - `description -> str`：Skill 描述。
      - `version -> str`：Skill 版本。
      - `digest -> str`：Skill 包摘要。
      - `category -> str`：分类标识，枚举为 `office`、`content`、`knowledge`、`data`、`education`、`agent`。
      - `category_name -> str`：分类中文名称，依次为 `办公效率`、`内容创作`、`知识管理`、`数据分析`、`教育学习`、`Agent 能力`。
      - `execution_mode -> str`：执行模式，取值为 `inline` 或 `sandbox`。
      - `requires -> list[str]`：运行依赖声明。
      - `allowed_tools -> list[str]`：允许调用的工具名称。
      - `enabled -> bool`：启用状态；目录接口固定为 `true`。

- **请求示例**：

  ```bash
  GET:/skills
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "items": [
              {
                  "name": "office-docs",
                  "description": "生成与处理办公文档",
                  "version": "1.0.0",
                  "digest": "3f2a1c9d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c",
                  "category": "office",
                  "category_name": "办公效率",
                  "execution_mode": "inline",
                  "requires": [],
                  "allowed_tools": ["deliver_file"],
                  "enabled": true
              }
          ]
      },
      "message": ""
  }
  ```

### 6.2 获取单个 Skill 模板详情

- **接口说明**：该接口用于获取单个 Skill 模板的元数据及其 `SKILL.md` 正文，供模板浏览页展示。模板不存在时返回 `Skill 模板不存在`。

- **接口信息**：`授权`+`GET:/skills/:skill_name`

- **接口参数**：

  - 请求参数：
    - `skill_name -> str`：路由参数，Skill 名称，类型为字符串。
  - 响应参数：
    - 其余字段与 6.1 的列表元素一致。
    - `content -> str`：该 Skill 的 `SKILL.md` 正文。

- **请求示例**：

  ```bash
  GET:/skills/office-docs
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "name": "office-docs",
          "description": "生成与处理办公文档",
          "version": "1.0.0",
          "digest": "3f2a1c9d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c",
          "category": "office",
          "category_name": "办公效率",
          "execution_mode": "inline",
          "requires": [],
          "allowed_tools": ["deliver_file"],
          "enabled": true,
          "content": "---\nname: office-docs\ndescription: 生成与处理办公文档\n---\n\n# 办公文档\n"
      },
      "message": ""
  }
  ```

### 6.3 获取指定应用的 Skill 目录

- **接口说明**：该接口用于获取指定应用可用的 Skill 目录，并附带该应用的启用状态。**只反映草稿配置中的绑定**，发布配置的绑定状态不通过该接口暴露。

- **接口信息**：`授权`+`GET:/apps/:app_id/skills`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
  - 响应参数：
    - `items -> list[dict]`：Skill 目录列表，字段与 6.1 一致，其中 `enabled` 表示该应用是否已启用该 Skill。

- **请求示例**：

  ```bash
  GET:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/skills
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "items": [
              {
                  "name": "office-docs",
                  "description": "生成与处理办公文档",
                  "version": "1.0.0",
                  "digest": "3f2a1c9d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c",
                  "category": "office",
                  "category_name": "办公效率",
                  "execution_mode": "inline",
                  "requires": [],
                  "allowed_tools": ["deliver_file"],
                  "enabled": true
              }
          ]
      },
      "message": ""
  }
  ```

---

## 07. Workspace 文件模块

Workspace 是账号与应用维度**跨会话共享**的文件空间，配额为 `WORKSPACE_MAX_BYTES`（默认 100 MiB）与 `WORKSPACE_MAX_FILES`（默认 1000）。虚拟路径以 `/workspace/` 开头，长度不超过 512，禁止使用 `\`、NUL、控制字符以及 `.`、`..`、`~` 路径片段。

**作用域差异需要注意**：`/apps/...` 路径族使用 `app.account_id(应用所有者)` 作为作用域账号；`/web-apps/...` 路径族使用 `current_user.id(当前登录账号)`。而 Agent 运行时始终以 `app.account_id` 读写。因此只有当登录账号恰好是应用所有者时，WebApp 路径族才能看到 Agent 产出的文件。

### 7.1 获取应用共享 Workspace 文件列表

- **接口说明**：该接口用于列出应用共享 Workspace 下的文件元数据，支持按路径前缀过滤。

- **接口信息**：`授权`+`GET:/apps/:app_id/workspace/files`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `path_prefix -> str`：可选参数，路径前缀过滤，长度不超过 512。
  - 响应参数：
    - `items -> list[dict]`：文件元数据列表。
      - `id -> uuid`：文件记录 id。
      - `app_id -> uuid`：应用 id。
      - `conversation_id -> uuid`：**Workspace 作用域 id，不是业务会话 id**（历史字段复用）。
      - `path -> str`：文件虚拟路径。
      - `revision -> int`：文件版本号。
      - `mime_type -> str`：MIME 类型。
      - `size_bytes -> int`：文件字节数。
      - `content_sha256 -> str`：内容摘要。
      - `created_by -> uuid | null`：创建者账号 id。
      - `created_at -> str | null`：创建时间，ISO 8601 字符串。
      - `updated_at -> str | null`：更新时间，ISO 8601 字符串。

- **请求示例**：

  ```bash
  GET:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/workspace/files?path_prefix=/workspace/reports
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "items": [
              {
                  "id": "b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e",
                  "app_id": "5e7834dc-bbca-4ee5-9591-8f297f5acded",
                  "conversation_id": "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f",
                  "path": "/workspace/reports/weekly.md",
                  "revision": 2,
                  "mime_type": "text/markdown",
                  "size_bytes": 2048,
                  "content_sha256": "3f2a1c9d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c",
                  "created_by": "c0759ca8-2d35-4480-83a8-1f41f29d1401",
                  "created_at": "2026-09-14T11:23:54",
                  "updated_at": "2026-09-14T11:24:12"
              }
          ]
      },
      "message": ""
  }
  ```

### 7.2 写入应用共享 Workspace 文件

- **接口说明**：该接口用于向应用共享 Workspace 写入文本文件。允许写入空内容，但**必须提供 `content` 字段且必须是字符串**，否则返回 `content 必须是字符串`。

- **接口信息**：`授权`+`POST:/apps/:app_id/workspace/files`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `path -> str`：必填参数，文件虚拟路径，不能为空，长度不超过 512。
    - `content -> str`：必填参数，文件文本内容，允许为空字符串。
    - `mime_type -> str`：可选参数，MIME 类型，默认为 `text/plain`，长度不超过 128。
  - 响应参数：与 7.1 中单个文件元数据结构一致。

- **请求示例**：

  ```bash
  POST:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/workspace/files
  
  {
      "path": "/workspace/reports/weekly.md",
      "content": "# 本周项目进展\n\n1. 完成 Agent 工作台重构",
      "mime_type": "text/markdown"
  }
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "id": "b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e",
          "app_id": "5e7834dc-bbca-4ee5-9591-8f297f5acded",
          "conversation_id": "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f",
          "path": "/workspace/reports/weekly.md",
          "revision": 1,
          "mime_type": "text/markdown",
          "size_bytes": 42,
          "content_sha256": "3f2a1c9d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c",
          "created_by": "c0759ca8-2d35-4480-83a8-1f41f29d1401",
          "created_at": "2026-09-14T11:23:54",
          "updated_at": "2026-09-14T11:23:54"
      },
      "message": ""
  }
  ```

### 7.3 预览应用共享 Workspace 文件

- **接口说明**：该接口用于读取文件元数据与文本内容预览，内容过长时会被截断，并通过 `truncated` 标识。

- **接口信息**：`授权`+`GET:/apps/:app_id/workspace/file`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `path -> str`：必填参数，文件虚拟路径，不能为空，长度不超过 512。
  - 响应参数：
    - 元数据字段与 7.1 一致。
    - `content -> str | null`：文件文本内容，二进制文件时为 `null`。
    - `download_url -> str | null`：带时效的下载地址。
    - `truncated -> bool`：内容是否被截断。

- **请求示例**：

  ```bash
  GET:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/workspace/file?path=/workspace/reports/weekly.md
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "id": "b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e",
          "app_id": "5e7834dc-bbca-4ee5-9591-8f297f5acded",
          "conversation_id": "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f",
          "path": "/workspace/reports/weekly.md",
          "revision": 1,
          "mime_type": "text/markdown",
          "size_bytes": 42,
          "content_sha256": "3f2a1c9d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c",
          "created_by": "c0759ca8-2d35-4480-83a8-1f41f29d1401",
          "created_at": "2026-09-14T11:23:54",
          "updated_at": "2026-09-14T11:24:12",
          "content": "# 本周项目进展\n\n1. 完成 Agent 工作台重构",
          "download_url": "https://bucket.cos.ap-guangzhou.myqcloud.com/workspace/reports/weekly.md?sign=q-sign-algorithm%3Dsha1",
          "truncated": false
      },
      "message": ""
  }
  ```

### 7.4 下载应用共享 Workspace 文件

- **接口说明**：该接口用于下载文件。**响应不是 JSON，而是 302 跳转到带时效的对象存储签名地址**（默认有效期 300 秒），由客户端或浏览器直接跟随跳转。

- **接口信息**：`授权`+`GET:/apps/:app_id/workspace/file/download`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `path -> str`：必填参数，文件虚拟路径，不能为空，长度不超过 512。
  - 响应参数：无响应体，返回 `302` 与 `Location` 响应头。

- **请求示例**：

  ```bash
  GET:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/workspace/file/download?path=/workspace/reports/weekly.md
  ```

- **响应示例**：

  ```
  HTTP/1.1 302 FOUND
  Location: https://bucket.cos.ap-guangzhou.myqcloud.com/workspace/reports/weekly.md?sign=q-sign-algorithm%3Dsha1
  ```

### 7.5 删除应用共享 Workspace 文件

- **接口说明**：该接口用于删除应用共享 Workspace 下的指定文件。

- **接口信息**：`授权`+`DELETE:/apps/:app_id/workspace/file`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `path -> str`：必填参数，文件虚拟路径，不能为空，长度不超过 512。
  - 响应参数：无数据，`data` 为空对象。

- **请求示例**：

  ```bash
  DELETE:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/workspace/file
  
  {
      "path": "/workspace/reports/weekly.md"
  }
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {},
      "message": "Workspace 文件已删除"
  }
  ```

### 7.6 检索 WebApp 共享 Workspace 文件

- **接口说明**：该接口用于按关键词检索当前登录账号在指定 WebApp 应用下的共享 Workspace 文件，用于"交付文件"聚合视图。

- **接口信息**：`授权`+`GET:/web-apps/:token/workspace/files`

- **接口参数**：

  - 请求参数：
    - `token -> str`：路由参数，WebApp 凭证标识，类型为字符串。
    - `query -> str`：可选参数，检索关键词，长度不超过 200。
  - 响应参数：
    - `items -> list[dict]`：文件元数据列表，字段与 7.1 一致。

- **请求示例**：

  ```bash
  GET:/web-apps/1550b71asdfd94/workspace/files?query=周报
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "items": [
              {
                  "id": "b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e",
                  "app_id": "5e7834dc-bbca-4ee5-9591-8f297f5acded",
                  "conversation_id": "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f",
                  "path": "/workspace/reports/weekly.md",
                  "revision": 1,
                  "mime_type": "text/markdown",
                  "size_bytes": 42,
                  "content_sha256": "3f2a1c9d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c",
                  "created_by": "c0759ca8-2d35-4480-83a8-1f41f29d1401",
                  "created_at": "2026-09-14T11:23:54",
                  "updated_at": "2026-09-14T11:24:12"
              }
          ]
      },
      "message": ""
  }
  ```

### 7.7 预览 WebApp 共享 Workspace 文件

- **接口说明**：该接口用于在 WebApp 路径族下预览共享 Workspace 文件。

- **接口信息**：`授权`+`GET:/web-apps/:token/workspace/file`

- **接口参数**：

  - 请求参数：
    - `token -> str`：路由参数，WebApp 凭证标识，类型为字符串。
    - `path -> str`：必填参数，文件虚拟路径，不能为空，长度不超过 512。
  - 响应参数：与 7.3 完全一致。

- **请求示例**：

  ```bash
  GET:/web-apps/1550b71asdfd94/workspace/file?path=/workspace/reports/weekly.md
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "id": "b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e",
          "app_id": "5e7834dc-bbca-4ee5-9591-8f297f5acded",
          "conversation_id": "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f",
          "path": "/workspace/reports/weekly.md",
          "revision": 1,
          "mime_type": "text/markdown",
          "size_bytes": 42,
          "content_sha256": "3f2a1c9d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c",
          "created_by": "c0759ca8-2d35-4480-83a8-1f41f29d1401",
          "created_at": "2026-09-14T11:23:54",
          "updated_at": "2026-09-14T11:24:12",
          "content": "# 本周项目进展\n\n1. 完成 Agent 工作台重构",
          "download_url": "https://bucket.cos.ap-guangzhou.myqcloud.com/workspace/reports/weekly.md?sign=q-sign-algorithm%3Dsha1",
          "truncated": false
      },
      "message": ""
  }
  ```

### 7.8 下载 WebApp 共享 Workspace 文件

- **接口说明**：该接口用于在 WebApp 路径族下下载共享 Workspace 文件，响应同样为 302 跳转。

- **接口信息**：`授权`+`GET:/web-apps/:token/workspace/file/download`

- **接口参数**：

  - 请求参数：
    - `token -> str`：路由参数，WebApp 凭证标识，类型为字符串。
    - `path -> str`：必填参数，文件虚拟路径，不能为空，长度不超过 512。
  - 响应参数：无响应体，返回 `302` 与 `Location` 响应头。

- **请求示例**：

  ```bash
  GET:/web-apps/1550b71asdfd94/workspace/file/download?path=/workspace/reports/weekly.md
  ```

- **响应示例**：

  ```
  HTTP/1.1 302 FOUND
  Location: https://bucket.cos.ap-guangzhou.myqcloud.com/workspace/reports/weekly.md?sign=q-sign-algorithm%3Dsha1
  ```

### 7.9 按会话预览 WebApp 共享 Workspace 文件

- **接口说明**：该接口是保留的兼容路由，用于按会话路径预览共享 Workspace 文件。**路径中的会话 id 只用于校验访问权限，实际读取的仍然是账号与应用维度的共享空间**，不是会话私有空间。

- **接口信息**：`授权`+`GET:/web-apps/:token/conversations/:conversation_id/file`

- **接口参数**：

  - 请求参数：
    - `token -> str`：路由参数，WebApp 凭证标识，类型为字符串。
    - `conversation_id -> uuid`：路由参数，会话 id，类型为 uuid。
    - `path -> str`：必填参数，文件虚拟路径，不能为空，长度不超过 512。
  - 响应参数：与 7.3 完全一致。

- **请求示例**：

  ```bash
  GET:/web-apps/1550b71asdfd94/conversations/2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8/file?path=/workspace/reports/weekly.md
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "id": "b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e",
          "app_id": "5e7834dc-bbca-4ee5-9591-8f297f5acded",
          "conversation_id": "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f",
          "path": "/workspace/reports/weekly.md",
          "revision": 1,
          "mime_type": "text/markdown",
          "size_bytes": 42,
          "content_sha256": "3f2a1c9d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c",
          "created_by": "c0759ca8-2d35-4480-83a8-1f41f29d1401",
          "created_at": "2026-09-14T11:23:54",
          "updated_at": "2026-09-14T11:24:12",
          "content": "# 本周项目进展\n\n1. 完成 Agent 工作台重构",
          "download_url": "https://bucket.cos.ap-guangzhou.myqcloud.com/workspace/reports/weekly.md?sign=q-sign-algorithm%3Dsha1",
          "truncated": false
      },
      "message": ""
  }
  ```

### 7.10 按会话下载 WebApp 共享 Workspace 文件

- **接口说明**：该接口是保留的兼容路由，用于按会话路径下载共享 Workspace 文件，响应为 302 跳转。

- **接口信息**：`授权`+`GET:/web-apps/:token/conversations/:conversation_id/file/download`

- **接口参数**：

  - 请求参数：
    - `token -> str`：路由参数，WebApp 凭证标识，类型为字符串。
    - `conversation_id -> uuid`：路由参数，会话 id，类型为 uuid。
    - `path -> str`：必填参数，文件虚拟路径，不能为空，长度不超过 512。
  - 响应参数：无响应体，返回 `302` 与 `Location` 响应头。

- **请求示例**：

  ```bash
  GET:/web-apps/1550b71asdfd94/conversations/2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8/file/download?path=/workspace/reports/weekly.md
  ```

- **响应示例**：

  ```
  HTTP/1.1 302 FOUND
  Location: https://bucket.cos.ap-guangzhou.myqcloud.com/workspace/reports/weekly.md?sign=q-sign-algorithm%3Dsha1
  ```

---

## 08. 长期记忆模块

长期记忆保存在 LangGraph Store 中，隔离身份为 `(账号 id, 应用 id)`，命名空间为 `("mydify", "agent-memory", <account_id>, <app_id>)`。**命名空间不含终端用户维度**，因此同一应用下的所有终端用户共享一份记忆。虚拟根目录为 `/memories/`，分类固定为 `preferences(用户偏好)`、`background(背景信息)`、`experience(经验方法)`；索引文件 `/memories/.index.md` 由系统生成，**只读**。

配额限制：单文件 64 KiB、文件数 100、总容量 1 MiB。

**所有记忆接口都要求应用已启用长期记忆**，否则返回 `该应用未启用长期记忆`。`/apps/...` 路径族读取**草稿配置**的开关，`/web-apps/...` 路径族读取**发布配置**的开关；两者的记忆作用域相同，都是 `(当前登录账号, 应用 id)`。

### 8.1 获取应用长期记忆文件列表

- **接口说明**：该接口用于列出当前创建者在指定应用下的长期记忆文件。

- **接口信息**：`授权`+`GET:/apps/:app_id/memory/files`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
  - 响应参数：
    - `items -> list[dict]`：记忆文件列表。
      - `path -> str`：文件虚拟路径。
      - `size_bytes -> int`：文件字节数。
      - `created_at -> str | null`：创建时间，ISO 8601 字符串。
      - `updated_at -> str | null`：更新时间，ISO 8601 字符串。
      - `content_sha256 -> str`：内容摘要，用于并发校验。
      - `preview -> str`：内容预览。

- **请求示例**：

  ```bash
  GET:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/memory/files
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "items": [
              {
                  "path": "/memories/.index.md",
                  "size_bytes": 420,
                  "created_at": "2026-09-14T11:23:54",
                  "updated_at": "2026-09-14T11:24:12",
                  "content_sha256": "3f2a1c9d4e5b6a7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c",
                  "preview": "- [用户偏好](preferences/style.md)：回答风格偏好"
              },
              {
                  "path": "/memories/preferences/style.md",
                  "size_bytes": 128,
                  "created_at": "2026-09-14T11:23:54",
                  "updated_at": "2026-09-14T11:24:12",
                  "content_sha256": "9f2c1a4b3d5e4f609a712b3c4d5e6f709a712b3c4d5e6f709a712b3c4d5e6f70",
                  "preview": "偏好简洁的回答风格。"
              }
          ]
      },
      "message": ""
  }
  ```

### 8.2 读取应用长期记忆文件

- **接口说明**：该接口用于读取单个长期记忆文件的元数据与完整文本内容。

- **接口信息**：`授权`+`GET:/apps/:app_id/memory/file`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `path -> str`：必填参数，文件虚拟路径，不能为空，长度不超过 512。
  - 响应参数：
    - 元数据字段与 8.1 一致。
    - `content -> str`：文件完整文本内容。

- **请求示例**：

  ```bash
  GET:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/memory/file?path=/memories/preferences/style.md
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "path": "/memories/preferences/style.md",
          "size_bytes": 128,
          "created_at": "2026-09-14T11:23:54",
          "updated_at": "2026-09-14T11:24:12",
          "content_sha256": "9f2c1a4b3d5e4f609a712b3c4d5e6f709a712b3c4d5e6f709a712b3c4d5e6f70",
          "preview": "偏好简洁的回答风格。",
          "content": "偏好简洁的回答风格，避免冗长的客套话。"
      },
      "message": ""
  }
  ```

### 8.3 下载应用长期记忆文件

- **接口说明**：该接口以 UTF-8 文本附件形式返回记忆文件，**响应不是 JSON 信封，而是 `text/plain` 附件**。

- **接口信息**：`授权`+`GET:/apps/:app_id/memory/file/download`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `path -> str`：必填参数，文件虚拟路径，不能为空，长度不超过 512。
  - 响应参数：无 JSON，返回 `Content-Type: text/plain` 的附件下载。

- **请求示例**：

  ```bash
  GET:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/memory/file/download?path=/memories/preferences/style.md
  ```

- **响应示例**：

  ```
  HTTP/1.1 200 OK
  Content-Type: text/plain; charset=utf-8
  Content-Disposition: attachment; filename=style.md

  偏好简洁的回答风格，避免冗长的客套话。
  ```

### 8.4 写入应用长期记忆文件

- **接口说明**：该接口用于创建或更新长期记忆文本文件。**必须提供 `content` 字段且必须是字符串**（允许空字符串），否则返回 `content 必须是字符串`。写入采用乐观并发控制：传入 `expected_sha256` 时，若文件当前摘要不匹配则拒绝覆盖，避免覆盖其他用户的修改。

- **接口信息**：`授权`+`POST:/apps/:app_id/memory/file`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `path -> str`：必填参数，目标文件虚拟路径，不能为空，长度不超过 512。
    - `content -> str`：必填参数，文件文本内容，允许为空字符串。
    - `original_path -> str`：可选参数，原文件路径，用于重命名或移动，长度不超过 512。
    - `expected_sha256 -> str`：可选参数，期望的文件当前摘要，必须是 64 位小写十六进制字符串。
  - 响应参数：字段与 8.2 一致。

- **请求示例**：

  ```bash
  POST:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/memory/file
  
  {
      "path": "/memories/preferences/style.md",
      "content": "偏好简洁的回答风格，避免冗长的客套话。",
      "expected_sha256": "9f2c1a4b3d5e4f609a712b3c4d5e6f709a712b3c4d5e6f709a712b3c4d5e6f70"
  }
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "path": "/memories/preferences/style.md",
          "size_bytes": 128,
          "created_at": "2026-09-14T11:23:54",
          "updated_at": "2026-09-14T11:24:12",
          "content_sha256": "1a2b3c4d5e6f70819a712b3c4d5e6f709a712b3c4d5e6f709a712b3c4d5e6f7081",
          "preview": "偏好简洁的回答风格，避免冗长的客套话。",
          "content": "偏好简洁的回答风格，避免冗长的客套话。"
      },
      "message": ""
  }
  ```

### 8.5 删除应用长期记忆文件

- **接口说明**：该接口用于删除指定的长期记忆文件。同样支持 `expected_sha256` 乐观并发校验。

- **接口信息**：`授权`+`DELETE:/apps/:app_id/memory/file`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `path -> str`：必填参数，文件虚拟路径，不能为空，长度不超过 512。
    - `expected_sha256 -> str`：可选参数，期望的文件当前摘要，必须是 64 位小写十六进制字符串。
  - 响应参数：无数据，`data` 为空对象。

- **请求示例**：

  ```bash
  DELETE:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/memory/file
  
  {
      "path": "/memories/preferences/style.md"
  }
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {},
      "message": "记忆文件已删除"
  }
  ```

### 8.6 获取 WebApp 长期记忆文件列表

- **接口说明**：该接口用于列出当前登录账号在指定 WebApp 应用下的长期记忆文件。**启用开关取自发布配置**，应用未发布或发布配置未启用长期记忆时返回 `该应用未启用长期记忆`。

- **接口信息**：`授权`+`GET:/web-apps/:token/memory/files`

- **接口参数**：

  - 请求参数：
    - `token -> str`：路由参数，WebApp 凭证标识，类型为字符串。
  - 响应参数：与 8.1 完全一致。

- **请求示例**：

  ```bash
  GET:/web-apps/1550b71asdfd94/memory/files
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "items": [
              {
                  "path": "/memories/preferences/style.md",
                  "size_bytes": 128,
                  "created_at": "2026-09-14T11:23:54",
                  "updated_at": "2026-09-14T11:24:12",
                  "content_sha256": "9f2c1a4b3d5e4f609a712b3c4d5e6f709a712b3c4d5e6f709a712b3c4d5e6f70",
                  "preview": "偏好简洁的回答风格。"
              }
          ]
      },
      "message": ""
  }
  ```

### 8.7 读取 WebApp 长期记忆文件

- **接口说明**：该接口用于读取 WebApp 应用下的单个长期记忆文件。

- **接口信息**：`授权`+`GET:/web-apps/:token/memory/file`

- **接口参数**：

  - 请求参数：
    - `token -> str`：路由参数，WebApp 凭证标识，类型为字符串。
    - `path -> str`：必填参数，文件虚拟路径，不能为空，长度不超过 512。
  - 响应参数：与 8.2 完全一致。

- **请求示例**：

  ```bash
  GET:/web-apps/1550b71asdfd94/memory/file?path=/memories/preferences/style.md
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "path": "/memories/preferences/style.md",
          "size_bytes": 128,
          "created_at": "2026-09-14T11:23:54",
          "updated_at": "2026-09-14T11:24:12",
          "content_sha256": "9f2c1a4b3d5e4f609a712b3c4d5e6f709a712b3c4d5e6f709a712b3c4d5e6f70",
          "preview": "偏好简洁的回答风格。",
          "content": "偏好简洁的回答风格，避免冗长的客套话。"
      },
      "message": ""
  }
  ```

### 8.8 下载 WebApp 长期记忆文件

- **接口说明**：该接口以 UTF-8 文本附件形式返回 WebApp 应用下的记忆文件。

- **接口信息**：`授权`+`GET:/web-apps/:token/memory/file/download`

- **接口参数**：

  - 请求参数：
    - `token -> str`：路由参数，WebApp 凭证标识，类型为字符串。
    - `path -> str`：必填参数，文件虚拟路径，不能为空，长度不超过 512。
  - 响应参数：无 JSON，返回 `Content-Type: text/plain` 的附件下载。

- **请求示例**：

  ```bash
  GET:/web-apps/1550b71asdfd94/memory/file/download?path=/memories/preferences/style.md
  ```

- **响应示例**：

  ```
  HTTP/1.1 200 OK
  Content-Type: text/plain; charset=utf-8
  Content-Disposition: attachment; filename=style.md

  偏好简洁的回答风格，避免冗长的客套话。
  ```

### 8.9 写入 WebApp 长期记忆文件

- **接口说明**：该接口用于创建或更新 WebApp 应用下的长期记忆文本文件，校验规则与 8.4 一致。

- **接口信息**：`授权`+`POST:/web-apps/:token/memory/file`

- **接口参数**：

  - 请求参数：
    - `token -> str`：路由参数，WebApp 凭证标识，类型为字符串。
    - `path -> str`：必填参数，目标文件虚拟路径，不能为空，长度不超过 512。
    - `content -> str`：必填参数，文件文本内容，允许为空字符串。
    - `original_path -> str`：可选参数，原文件路径，长度不超过 512。
    - `expected_sha256 -> str`：可选参数，期望的文件当前摘要，必须是 64 位小写十六进制字符串。
  - 响应参数：与 8.2 完全一致。

- **请求示例**：

  ```bash
  POST:/web-apps/1550b71asdfd94/memory/file
  
  {
      "path": "/memories/background/project.md",
      "content": "当前项目是一个 Agent 平台。"
  }
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "path": "/memories/background/project.md",
          "size_bytes": 96,
          "created_at": "2026-09-14T11:25:00",
          "updated_at": "2026-09-14T11:25:00",
          "content_sha256": "2b3c4d5e6f708192a712b3c4d5e6f709a712b3c4d5e6f709a712b3c4d5e6f708192",
          "preview": "当前项目是一个 Agent 平台。",
          "content": "当前项目是一个 Agent 平台。"
      },
      "message": ""
  }
  ```

### 8.10 删除 WebApp 长期记忆文件

- **接口说明**：该接口用于删除 WebApp 应用下的指定长期记忆文件。

- **接口信息**：`授权`+`DELETE:/web-apps/:token/memory/file`

- **接口参数**：

  - 请求参数：
    - `token -> str`：路由参数，WebApp 凭证标识，类型为字符串。
    - `path -> str`：必填参数，文件虚拟路径，不能为空，长度不超过 512。
    - `expected_sha256 -> str`：可选参数，期望的文件当前摘要，必须是 64 位小写十六进制字符串。
  - 响应参数：无数据，`data` 为空对象。

- **请求示例**：

  ```bash
  DELETE:/web-apps/1550b71asdfd94/memory/file
  
  {
      "path": "/memories/background/project.md"
  }
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {},
      "message": "记忆文件已删除"
  }
  ```

---

## 09. Agent 测评模块

Agent 测评用于对已完成的历史任务或重新执行的任务按评分标准打分。测评使用独立的执行图，整体时限为 900 秒，通过后台队列异步执行。提交接口需要**历史任务、评分模型和 1–12 条不重复标准**，送交评分的文本超过 48000 字时会被拒绝，不做截断。

**本模块的分页响应不使用 `paginator` 包装**，而是平铺 `total`、`current_page` 和 `page_size` 三个字段。

### 9.1 获取可选的历史任务列表

- **接口说明**：该接口用于获取指定应用下可用于测评的历史任务分页列表。**只有已结束（`completed`、`failed`、`cancelled`）且来源为 `debugger` 或 `creator_task` 的运行才会出现**。列表中的 `query` 会被截断到 160 字。

- **接口信息**：`授权`+`GET:/apps/:app_id/evaluation-sources`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `current_page -> int`：可选参数，当前页数，默认为 1，范围为 1–9999。
    - `page_size -> int`：可选参数，每页数据条数，默认为 20，范围为 1–50。
  - 响应参数：
    - `list -> list[dict]`：历史任务列表。
      - `run_id -> uuid`：运行 id。
      - `conversation_id -> uuid`：会话 id。
      - `query -> str`：用户提问，列表中最多返回前 160 字。
      - `answer -> str`：AI 回答，列表接口不裁剪该字段，但历史列表按 9.4 的规则省略。
      - `status -> str`：运行状态。
      - `invoke_from -> str`：调用来源。
      - `created_at -> str | null`：创建时间。
      - `finished_at -> str | null`：结束时间。
      - `scope -> str`：测评材料范围说明。
      - `execution -> dict`：执行信息。
    - `total -> int`：总记录条数。
    - `current_page -> int`：当前的页数。
    - `page_size -> int`：每页的条数。

- **请求示例**：

  ```bash
  GET:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/evaluation-sources?current_page=1&page_size=20
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "list": [
              {
                  "run_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70",
                  "conversation_id": "2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8",
                  "query": "帮我汇总本周的项目进展",
                  "answer": "本周共完成 3 项任务。",
                  "status": "completed",
                  "invoke_from": "debugger",
                  "created_at": "2026-09-14T11:23:54",
                  "finished_at": "2026-09-14T11:24:12",
                  "scope": "query_and_answer",
                  "execution": {}
              }
          ],
          "total": 1,
          "current_page": 1,
          "page_size": 20
      },
      "message": ""
  }
  ```

### 9.2 获取指定历史任务的测评原文

- **接口说明**：该接口用于查看将实际送交评分的完整文本，便于用户确认测评材料。任务不存在、尚未结束或不属于当前账号时返回 `任务不存在、尚未结束或无权测评`。

- **接口信息**：`授权`+`GET:/apps/:app_id/evaluation-sources/:run_id`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `run_id -> uuid`：路由参数，运行 id，类型为 uuid。
  - 响应参数：与 9.1 中 `list` 的单个元素结构一致，其中 `query` 与 `answer` 返回完整文本。

- **请求示例**：

  ```bash
  GET:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/evaluation-sources/8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "run_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70",
          "conversation_id": "2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8",
          "query": "帮我汇总本周的项目进展",
          "answer": "本周共完成 3 项任务。",
          "status": "completed",
          "invoke_from": "debugger",
          "created_at": "2026-09-14T11:23:54",
          "finished_at": "2026-09-14T11:24:12",
          "scope": "query_and_answer",
          "execution": {}
      },
      "message": ""
  }
  ```

### 9.3 新建 Agent 测评

- **接口说明**：该接口用于提交一次测评。调用者只能选择历史运行和已有模型连接，**不能传入伪造的任务结果**。请求带 `request_id` 时具备幂等性：同一 `request_id` 重复提交返回同一条测评记录，若该标识已用于不同的测评内容则返回 `提交标识已用于不同的测评，请重新提交`。测评在后台队列执行，提交后需要通过详情接口轮询结果。

- **接口信息**：`授权`+`POST:/apps/:app_id/evaluations`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `run_id -> uuid`：必填参数，被测评的历史运行 id。
    - `request_id -> uuid`：可选参数，提交标识，用于幂等去重。
    - `judge -> dict`：必填参数，评分模型，结构为 `{provider, model, provider_connection_id}`，只能引用已有模型连接。
    - `mode -> str`：可选参数，测评模式，枚举为 `historical(默认，评测已有结果)` 或 `execute(重新执行后评测)`。
    - `max_iterations -> int`：可选参数，最大迭代轮数，默认为 3，范围为 1–5。
    - `criteria -> list[str]`：必填参数，评分标准，数量 1–12，每条长度 1–500，且不能重复。
    - `evidence_rules -> list[dict]`：可选参数，确定性证据核验规则，最多 8 条。
      - `kind -> str`：必填参数，核验类型，枚举为 `contains(文本包含)`、`json_keys(JSON 顶层字段存在)`、`file_exists(虚拟文件存在)`。
      - `value -> str`：必填参数，核验目标，长度 1–500。`file_exists` 必须使用虚拟空间中的绝对路径（如 `/report.md`），不能包含 `..` 或 `\`。
  - 响应参数：
    - `id -> uuid`：测评记录 id。
    - `app_id -> uuid`：应用 id。
    - `run_id -> uuid`：被测评的运行 id。
    - `source_snapshot -> dict`：提交时的任务快照，结构同 9.2。
    - `criteria -> list[str]`：评分标准列表。
    - `judge_config -> dict`：评分模型配置。
    - `evaluator_version -> str`：评分实现版本。
    - `status -> str`：测评状态。
    - `result -> dict | null`：评分结果，执行完成前为 `null`。
    - `error_message -> str`：失败原因。
    - `created_at -> str`：创建时间，ISO 8601 字符串。
    - `finished_at -> str | null`：完成时间。

  请求校验失败时统一返回 `请选择历史任务、评分模型，并填写 1–12 条不重复标准，每条最多 500 字`。送交评分的文本超过 48000 字时返回 `本轮文本超过 48000 字，暂不支持测评；不会截断后评分`。后台队列不可用时测评记录会写入 `测评队列暂不可用，请稍后新建测评。`。

- **请求示例**：

  ```bash
  POST:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/evaluations
  
  {
      "run_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70",
      "request_id": "b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e",
      "judge": {
          "provider": "openai",
          "model": "gpt-4o-mini",
          "provider_connection_id": "c0759ca8-2d35-4480-83a8-1f41f29d1401"
      },
      "mode": "historical",
      "max_iterations": 3,
      "criteria": [
          "覆盖本周全部关键进展",
          "结论有明确数据支撑"
      ],
      "evidence_rules": [
          {
              "kind": "contains",
              "value": "本周"
          }
      ]
  }
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "id": "d5e6f708-192a-4b3c-8d4e-5f60718293a4",
          "app_id": "5e7834dc-bbca-4ee5-9591-8f297f5acded",
          "run_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70",
          "source_snapshot": {
              "run_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70",
              "conversation_id": "2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8",
              "query": "帮我汇总本周的项目进展",
              "answer": "本周共完成 3 项任务。",
              "status": "completed",
              "invoke_from": "debugger",
              "created_at": "2026-09-14T11:23:54",
              "finished_at": "2026-09-14T11:24:12",
              "scope": "query_and_answer",
              "execution": {}
          },
          "criteria": [
              "覆盖本周全部关键进展",
              "结论有明确数据支撑"
          ],
          "judge_config": {
              "provider": "openai",
              "model": "gpt-4o-mini",
              "provider_connection_id": "c0759ca8-2d35-4480-83a8-1f41f29d1401"
          },
          "evaluator_version": "answer-rubric-v3",
          "status": "pending",
          "result": null,
          "error_message": "",
          "created_at": "2026-09-14T11:25:00",
          "finished_at": null
      },
      "message": ""
  }
  ```

### 9.4 获取测评历史分页列表

- **接口说明**：该接口用于获取指定应用的测评分页历史。**列表会省略原始长文本**（`source_snapshot.answer` 被排除），并把 `result` 裁剪为只保留 `runtime` 的 `phase`、`status`、`accepted` 字段以及空的 `rounds` 数组，完整证据需通过详情接口读取。查询前会先把超过 15 分钟仍未结束的测评标记为失败。

- **接口信息**：`授权`+`GET:/apps/:app_id/evaluations`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `current_page -> int`：可选参数，当前页数，默认为 1，范围为 1–9999。
    - `page_size -> int`：可选参数，每页数据条数，默认为 20，范围为 1–50。
  - 响应参数：
    - `list -> list[dict]`：测评记录列表，字段与 9.3 的响应一致，但 `source_snapshot.answer` 被省略，`result` 被裁剪。
    - `total -> int`：总记录条数。
    - `current_page -> int`：当前的页数。
    - `page_size -> int`：每页的条数。

- **请求示例**：

  ```bash
  GET:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/evaluations?current_page=1&page_size=20
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "list": [
              {
                  "id": "d5e6f708-192a-4b3c-8d4e-5f60718293a4",
                  "app_id": "5e7834dc-bbca-4ee5-9591-8f297f5acded",
                  "run_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70",
                  "source_snapshot": {
                      "run_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70",
                      "conversation_id": "2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8",
                      "query": "帮我汇总本周的项目进展",
                      "status": "completed",
                      "invoke_from": "debugger",
                      "created_at": "2026-09-14T11:23:54",
                      "finished_at": "2026-09-14T11:24:12",
                      "scope": "query_and_answer",
                      "execution": {}
                  },
                  "criteria": [
                      "覆盖本周全部关键进展",
                      "结论有明确数据支撑"
                  ],
                  "judge_config": {
                      "provider": "openai",
                      "model": "gpt-4o-mini",
                      "provider_connection_id": "c0759ca8-2d35-4480-83a8-1f41f29d1401"
                  },
                  "evaluator_version": "answer-rubric-v3",
                  "status": "completed",
                  "result": {
                      "runtime": {
                          "phase": "completed",
                          "status": "completed",
                          "accepted": true,
                          "rounds": []
                      }
                  },
                  "error_message": "",
                  "created_at": "2026-09-14T11:25:00",
                  "finished_at": "2026-09-14T11:25:30"
              }
          ],
          "total": 1,
          "current_page": 1,
          "page_size": 20
      },
      "message": ""
  }
  ```

### 9.5 获取测评详情

- **接口说明**：该接口用于获取单条测评记录的完整快照与逐项评分结论，是轮询测评结果的主要接口。记录不存在时返回 `测评记录不存在`。

- **接口信息**：`授权`+`GET:/apps/:app_id/evaluations/:evaluation_id`

- **接口参数**：

  - 请求参数：
    - `app_id -> uuid`：路由参数，应用 id，类型为 uuid。
    - `evaluation_id -> uuid`：路由参数，测评记录 id，类型为 uuid。
  - 响应参数：与 9.3 的响应结构一致，`source_snapshot` 与 `result` 返回完整内容。

- **请求示例**：

  ```bash
  GET:/apps/5e7834dc-bbca-4ee5-9591-8f297f5acded/evaluations/d5e6f708-192a-4b3c-8d4e-5f60718293a4
  ```

- **响应示例**：

  ```json
  {
      "code": "success",
      "data": {
          "id": "d5e6f708-192a-4b3c-8d4e-5f60718293a4",
          "app_id": "5e7834dc-bbca-4ee5-9591-8f297f5acded",
          "run_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70",
          "source_snapshot": {
              "run_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70",
              "conversation_id": "2d7d3e3f-95c9-4d9d-ba9c-9daaf09cc8a8",
              "query": "帮我汇总本周的项目进展",
              "answer": "本周共完成 3 项任务。",
              "status": "completed",
              "invoke_from": "debugger",
              "created_at": "2026-09-14T11:23:54",
              "finished_at": "2026-09-14T11:24:12",
              "scope": "query_and_answer",
              "execution": {}
          },
          "criteria": [
              "覆盖本周全部关键进展",
              "结论有明确数据支撑"
          ],
          "judge_config": {
              "provider": "openai",
              "model": "gpt-4o-mini",
              "provider_connection_id": "c0759ca8-2d35-4480-83a8-1f41f29d1401"
          },
          "evaluator_version": "answer-rubric-v3",
          "status": "completed",
          "result": {
              "checks": [
                  {
                      "criterion_id": 1,
                      "verdict": "passed",
                      "reason": "回答明确给出了本周完成的任务数量。",
                      "evidence_source": "answer",
                      "evidence_quote": "本周共完成 3 项任务。",
                      "suggestion": "",
                      "score": 1.0
                  }
              ]
          },
          "error_message": "",
          "created_at": "2026-09-14T11:25:00",
          "finished_at": "2026-09-14T11:25:30"
      },
      "message": ""
  }
  ```

---

## Agent 项目扩展资料

### 01. 工作台命令与事件数据结构

工作台事件（`WorkbenchEvent`）是所有实时事件、历史事件和运行详情的统一结构：

```json
{
    "type": "event",
    "event_id": "8f2c1a4b-3d5e-4f60-9a71-2b3c4d5e6f70:3",
    "seq": 3,
    "method": "messages",
    "params": {
        "namespace": [],
        "timestamp": 1714053834500,
        "node": null,
        "data": {},
        "metadata": {
            "my_dify": {
                "source": "agent_v2",
                "source_seq": 5
            }
        }
    }
}
```

- `event_id` 格式固定为 `<run_id>:<seq>`，可直接作为 `Last-Event-ID` 续传游标。
- `method` 决定 `params.data` 的结构，取值共 6 种：

| `method` | 含义 | `params.data` 主要字段 |
|---|---|---|
| `messages` | 消息与推理增量 | `event`（`message-start`/`content-block-start`/`content-block-delta`/`content-block-finish`/`message-finish`）、`role`、`id`、`delta` |
| `values` | 会话状态快照 | `todos`、`files`、`async_tasks`、`pending_inputs`、`input_history`、`input_recovery`、`messages` |
| `tools` | 工具生命周期 | `event`（`tool-started`/`tool-output-delta`/`tool-finished`/`tool-error`）、`tool_call_id`、`tool_name`、`input`、`delta`、`output`、`message` |
| `lifecycle` | 运行生命周期 | `event`（`running`/`completed`/`failed`/`interrupted`/`drained`/`cancelled`）、`status`、`phase`、`reason`、`message_id`、`input_message_id`、`error`、`code` |
| `custom` | 上下文窗口观测 | `name`（`context_window`/`context_compaction`）、`payload` |
| `input.requested` | 待填表单 | `interrupt_id`、`payload`（表单定义） |

- 生命周期事件的特殊取值：等待用户输入时 `event = "interrupted"`、`reason = "user_input_required"`；取消时 `event = "interrupted"`、`reason = "cancel_requested"`；失败时额外带 `error`（默认 `Agent 运行失败`）与 `code`。
- `metadata.my_dify.source` 标识事件来源：`agent_v2(内核投影事件)` 或 `workbench(平台业务事务生成)`；来自内核的事件额外带 `source_seq`。
- `custom` 通道有严格白名单，只有 `context_window` 和 `context_compaction` 两类载荷会被发布；`checkpoints` 通道当前不发布任何事件。

**待填表单（`payload`）结构**：

```json
{
    "title": "补充周报范围",
    "description": "",
    "fields": [
        {
            "name": "report_scope",
            "label": "统计范围",
            "type": "select",
            "required": true,
            "options": [
                {
                    "value": "week",
                    "label": "本周",
                    "description": "",
                    "recommended": true
                }
            ],
            "description": "",
            "placeholder": "",
            "allow_other": false,
            "allow_skip": false,
            "allow_unavailable": true,
            "allow_delegate": false,
            "min_length": 0,
            "max_length": 4000,
            "min_value": null,
            "max_value": null,
            "integer_only": false
        }
    ]
}
```

- `fields` 数量为 1–8，字段 `name` 必须匹配 `^[a-zA-Z][a-zA-Z0-9_]{0,63}$` 且不能重复。
- `type` 枚举为 `text`、`textarea`、`select`、`multiselect`、`number`；只有选择控件支持 `options` 和 `allow_other`，只有数字控件支持 `min_value`、`max_value`、`integer_only`。
- 答案除直接值外，还支持结构化状态：`{"status": "skipped"}`、`{"status": "unavailable"}`、`{"status": "delegate"}`，以及自由补充 `{"status": "custom", "text": "...", "selected": [...]}`；可用性由表单字段的 `allow_skip`、`allow_unavailable`、`allow_delegate`、`allow_other` 决定。

### 02. 错误文案速查

工作台命令响应 `message` 字段的取值（均为 HTTP 200 + `type: "error"`）：

| 场景 | 文案 |
|---|---|
| 同一会话已有运行中任务 | `当前任务已有正在执行的 Agent` |
| 同一会话停在表单 | `请先完成当前表单` |
| 前序任务正在停止 | `当前任务正在停止，请稍后重试` |
| 表单被取消，本入口可恢复 | `此任务已在表单处停止，请点击重新填写后继续，或创建新会话` |
| 表单被取消，其他入口不可恢复 | `此任务已在表单处停止，请创建新会话` |
| 输入消息被其他入口占用 | `输入消息已用于其他调用入口` |
| 答案保存竞态 | `任务正在保存，请稍后重试` |
| 提交时应用配置已变更 | `应用配置已变更，请恢复原配置后继续填写，或创建新会话` |
| 恢复表单时配置已变更 | `应用配置已变更，请恢复原配置后重新填写，或创建新会话` |
| 表单重复修改 | `该表单已提交，不能重复修改` |
| 恢复时前序任务未停止 | `任务正在停止，请稍后重试` |
| `replace_run` 等待超时 | `前序任务尚未停止，本次更新未启动，请稍后重试` |

其余主要失败文案：

| 文案 | 来源 |
|---|---|
| `工作台命令必须是 JSON 对象` | 工作台命令校验（平台信封 `validate_error`） |
| `工作台命令格式错误: <详情>` | 工作台命令校验 |
| `不支持的工作台命令` | 工作台命令校验 |
| `run.start 的 input 必须是对象` | 工作台命令校验 |
| `run.start 需要 input.messages 或 query` | 工作台命令校验 |
| `run.start 需要非空的用户输入` | 工作台命令校验 |
| `工作台输入格式错误: <详情>` | 工作台命令校验 |
| `表单提交格式错误` / `表单恢复格式错误` | 工作台命令校验 |
| `任务状态已更新，请刷新后重新操作` | 表单恢复 |
| `此任务没有保留待填写内容，请创建新会话` | 表单恢复 |
| `仅支持恢复当前会话最新停止的表单，请刷新后重试` | 表单恢复 |
| `待回答任务不存在` | 提交表单答案 |
| `工作台订阅参数错误: <详情>` | SSE 订阅 |
| `执行详情分页参数无效` | 运行事件分页 |
| `Agent Run 不存在或不属于当前会话` / `Agent Run 不存在` | 运行查询 |
| `会话不存在或已删除` | 会话锁定 |
| `当前 Agent 执行繁忙，请稍后重试` | 执行槽已满（**平台信封异常，不是命令错误**） |
| `内部 Agent Protocol 凭证无效` / `内部 Agent Protocol 凭证已过期` | 内部凭证 |
| `内部 Agent Protocol 密钥未配置` | 内部凭证签发 |
| `内部 Agent Protocol 调用不在有效应用范围内` | 内部协议授权 |
| `内部 Agent Protocol 会话不存在或无权访问` | 内部协议会话 |
| `内部 Agent Protocol Run 不存在` | 内部协议运行 |
| `内部 Agent Protocol graph_id 不在授权范围内` | 内部协议创建运行 |
| `内部 Agent Protocol 请求格式错误: <详情>` | 内部协议请求体 |
| `后台 Run 缺少 input` / `后台 Run 输入格式错误: <详情>` | 内部协议创建运行 |
| `Run 必须且只能提供 input 或 command` | 内部协议创建运行 |
| `应用不存在或无权访问` | 应用权限校验 |
| `应用不存在、未发布或无权访问` | 任务中心 |
| `当前应用未发布，请核实后重试` | 取消发布 |
| `应用未发布，无法生成WebApp凭证标识` | 重新生成 WebApp 凭证 |
| `该应用未启用长期记忆` | 长期记忆 |
| `记忆索引由系统生成，请编辑分类下的记忆文件` | 长期记忆写入 |
| `Skill 模板不存在` | Skill 目录 |
| `Skill 不存在或未内置: <name>` / `Skill 版本不匹配: <name>` | Skill 绑定校验 |
| `所选 Skill 未挂载或未启用: <详情>` | Skill 运行校验 |
| `path 不能为空` / `content 必须是字符串` | Workspace 与记忆文件 |
| `请选择历史任务、评分模型，并填写 1–12 条不重复标准，每条最多 500 字` | 新建测评 |
| `任务不存在、尚未结束或无权测评` | 测评材料 |
| `测评记录不存在` | 测评详情 |
| `本轮文本超过 48000 字，暂不支持测评；不会截断后评分` | 新建测评 |
| `测评数据库结构尚未更新，请联系管理员完成数据库迁移后重试。` | 测评执行 |
| `测评等待或执行超过 15 分钟，请检查后台队列后新建测评。` | 测评超时收敛 |

### 03. 限流配置速查

限流器基于 Redis，**Redis 不可用时直接跳过限流并记录 `限流器不可用，跳过限流检查` 日志**。触发限流时返回 HTTP `429`，并在 `Retry-After` 响应头中给出建议重试秒数。

| 配置键 | 上限 | 窗口 | 策略 | Agent 相关使用位置 |
|---|---|---|---|---|
| `user` | 40 | 60 秒 | 滑动窗口 | `POST:/apps`、`POST:/apps/:app_id`、`POST:/apps/:app_id/copy`、`POST:/apps/:app_id/draft-app-config`、`POST:/apps/:app_id/fallback-history`、`POST:/assistant-agent/conversations` |
| `chat` | 12 | 60 秒 | 令牌桶 | `POST:/apps/:app_id/evaluations` |
| `sensitive` | 3 | 60 秒 | 固定窗口 | `POST:/apps/:app_id/delete`、`POST:/apps/:app_id/publish`、`POST:/apps/:app_id/cancel-publish`、`POST:/apps/:app_id/published-config/regenerate-web-app-token` |
| `database_intensive` | 25 | 60 秒 | 滑动窗口 | `GET:/apps`、`GET:/apps/:app_id/publish-histories` |
| `lightweight` | 60 | 60 秒 | 固定窗口 | `GET:/apps/:app_id`、`GET:/apps/:app_id/published-subagents`、`GET:/apps/:app_id/draft-app-config`、`GET:/apps/:app_id/published-config` |
| `ip` | 20 | 60 秒 | 滑动窗口 | 未登录场景 |
| `upload` | 6 | 60 秒 | 固定窗口 | 文件上传 |
| `retrieval` | 18 | 60 秒 | 滑动窗口 | 知识库检索 |
| `workflow` | 10 | 60 秒 | 令牌桶 | 工作流执行 |

**工作台五个端点、运行事件、Skill、Workspace、长期记忆、Creator 任务中心与内部 Agent Protocol 均未配置限流。**
