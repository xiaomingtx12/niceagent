# Agent V2 后端 Service / Handler 整理方案

本文是前端适配之前的后端业务层和 HTTP 接口层整理基线。
当前只描述改造边界、问题和实施顺序，不代表所有接口已经完成重构。

## 1. 当前结论

后端目前已经完成了 Agent V2 运行时的主要装配方向：

- `BaseAgent` 负责把平台配置转换为 DeepAgents 应用；
- `AgentRuntimeService` 负责 Checkpointer、Store、Backend、Skill 和运行时
  Context 的装配；
- DeepAgents 原生负责 State、Todo、Filesystem、SubAgent、Memory 和摘要；
- SSE 事件通过 `QueueEvent` 对外输出；
- 业务消息和 Agent 轨迹仍然写入 `Message` 和 `MessageAgentThought`。

但后端业务接口层还不能称为最终完成，主要原因是：

- 调试、WebApp、OpenAPI、辅助 Agent 各自重复实现了一套 Agent 调用流程；
- `AppService` 同时承担应用生命周期、配置校验、工具装配和调试对话；
- 部分 Handler 仍然包含运行前初始化、数据库查询和权限判断；
- 配置查询中存在跨租户查询范围不完整的问题；
- Workspace、Skill 和会话文件接口还没有统一成稳定的业务边界；
- SSE 事件聚合、持久化和错误收尾逻辑没有集中管理。

因此，前端适配应等待本方案中的核心接口稳定后再开始。

## 2. 目标分层

```text
HTTP Handler
    - 登录、限流、请求解析、Schema 校验、响应封装
    - 不创建 Agent，不查询业务表，不执行跨模块业务规则

Domain Service
    - 资源归属、租户权限、状态转换、事务边界
    - 业务配置规范化和资源绑定校验

Agent Invocation Service
    - 创建消息
    - 组装模型、工具和 Agent V2 Runtime
    - 执行 graph.stream / graph.invoke
    - 统一 SSE 事件、停止、错误和最终持久化

AgentRuntimeService
    - 只负责 DeepAgents 运行时资源装配
    - 不负责业务消息、不负责 HTTP、不负责应用配置生命周期

ConversationService
    - 会话和消息归属
    - Agent 轨迹持久化
    - 会话名称生成
    - 不负责创建 Agent 或选择工具
```

项目不新增 `AgentRuntimePlan`、自定义 Agent State、历史数组、迭代计数器、
第二套摘要机制或第二套记忆机制。

## 3. 模块职责调整

### 3.1 应用配置和应用生命周期

| 文件 | 保留职责 | 需要调整 |
|---|---|---|
| `internal/service/app_service.py` | 应用 CRUD、复制、发布、取消发布、发布历史、WebApp Token | 移除 Agent 执行代码和大段配置校验 |
| `internal/service/app_config_service.py` | 草稿/运行配置读取、规范化、资源绑定、工具转换 | 收拢全部配置校验；统一租户范围和绑定结果 |
| `internal/handler/app_handler.py` | 应用 HTTP 接口、Schema、响应 | 保持薄 Handler；调试聊天改为调用统一 Agent Invocation Service |
| `internal/schema/app_schema.py` | 应用请求和响应结构 | 将草稿配置请求拆成明确的配置 Schema，避免 Handler 直接接收无约束 JSON |

应用配置的职责边界：

```text
AppService
    应用本身、草稿版本、发布版本、发布历史、Token

AppConfigService
    model_config、tools、mcp_tools、workflows、datasets、skills、subagents
    retrieval_config、review_config、语音配置和前端展示配置
```

`opening_statement` 和 `opening_questions` 只属于应用展示配置，不能进入
Agent system prompt。Agent system prompt 只使用应用说明和用户预设提示词。

### 3.2 Agent 调用入口

新增一个业务层统一入口：

```text
internal/service/agent_invocation_service.py
```

它不是第二个 Agent 框架，而是把现有四个入口的重复业务流程集中起来：

- `AppService.debug_chat`
- `WebAppService.web_app_chat`
- `OpenAPIService.chat`
- `AssistantAgentService.chat`

统一服务负责：

1. 接收已经完成权限校验的调用上下文；
2. 创建或接收 `Conversation` 和 `Message`；
3. 加载模型；
4. 由 `AppConfigService` 转换平台工具；
5. 追加知识库检索工具和已发布工作流工具；
6. 调用 `AgentRuntimeService.prepare()`；
7. 构造 `BaseAgent`；
8. 统一处理流式事件聚合、SSE 包装和最终持久化；
9. 在异常、停止、客户端断开时完成资源和消息状态收尾。

四类入口只保留差异化的身份解析：

| 入口 | 入口服务负责的差异 |
|---|---|
| 调试 | 草稿配置、Creator 账号、`InvokeFrom.DEBUGGER` |
| WebApp | 发布配置、EndUser、`InvokeFrom.WEB_APP` |
| OpenAPI | API Key、EndUser、`InvokeFrom.SERVICE_API` |
| 辅助 Agent | 固定应用、辅助工具和 `InvokeFrom.ASSISTANT_AGENT` |

`AgentRuntimeService` 不扩展为聊天业务服务，继续只负责 DeepAgents 资源。

### 3.3 会话和消息

| 文件 | 处理范围 |
|---|---|
| `internal/service/conversation_service.py` | 会话归属、消息列表、删除、改名、置顶、轨迹持久化 |
| `internal/service/agent_conversation_cleanup_service.py` | 删除会话/消息时清理 Checkpoint、Workspace 和 Sandbox 资源 |
| `internal/handler/conversation_handler.py` | 仅处理分页参数、名称/置顶请求和响应 |

需要修正的重点：

- Agent 轨迹持久化必须保存事件真实的 `task_id`，不能误写
  `background_task_id`；
- 事件聚合逻辑从各个聊天 Service 移到统一 Invocation Service；
- 消息状态必须覆盖正常结束、错误、停止、超时和客户端断开；
- 删除调试会话时必须调用清理服务，而不是只把
  `debug_conversation_id` 置空；
- 长期记忆仍由 DeepAgents StoreBackend 管理，`Conversation.summary` 不再
  作为第二套长期记忆来源。

### 3.4 WebApp、OpenAPI 和辅助 Agent

| 文件 | 保留职责 | 需要调整 |
|---|---|---|
| `internal/service/web_app_service.py` | Token 校验、EndUser 解析、会话列表、WebApp 展示配置 | 聊天执行交给 Invocation Service |
| `internal/service/openapi_service.py` | API 请求身份、应用发布状态、EndUser/Conversation 归属 | 聊天执行交给 Invocation Service |
| `internal/service/assistant_agent_service.py` | 辅助 Agent 专用工具和固定应用身份 | 聊天执行交给 Invocation Service；移除 Handler 中的初始化逻辑 |
| `internal/handler/web_app_handler.py` | 请求校验和响应 | 不直接参与 Agent 组装 |
| `internal/handler/openapi_handler.py` | OpenAPI 请求校验和响应 | 不直接参与 Agent 组装 |
| `internal/handler/assistant_agent_handler.py` | 辅助 Agent 请求校验和响应 | 不初始化 Embeddings/FAISS，不读取 API Key |

辅助 Agent 的 Embeddings/FAISS 初始化属于 `AssistantAgentService` 或其
专用资源工厂，不能放在 Handler。

### 3.5 Skill 和 Workspace

| 文件 | 目标职责 |
|---|---|
| `internal/service/skill_service.py` | Skill 目录扫描、版本选择、绑定规范化、主 Agent/SubAgent 子集校验 |
| `internal/handler/skill_handler.py` | 仅返回当前 App 可见 Skill 目录 |
| `internal/service/workspace_file_service.py` | 文件路径规范化、租户/App/Conversation 归属、版本、配额、软删除 |
| `internal/handler/workspace_file_handler.py` | 仅解析路径和内容，调用 WorkspaceFileService |

WebApp 会话归属查询不能长期放在 `WorkspaceFileHandler._owned_web_conversation`
中，应移动到 `WebAppService` 或 WorkspaceFileService 的业务方法中。

## 4. 必须先修正的问题

### P0：跨租户和跨应用边界

1. `AppConfigService` 查询无效知识库关联时，删除关联记录必须同时限制
   `app_id`，不能只按 `dataset_id` 删除。
2. API Tool 的查询和绑定校验必须限制当前账号或当前应用所有者。
3. MCP Server、MCP Tool、Workflow、Dataset 和 Skill 的绑定读取都必须经过
   当前 App/Account 的归属校验。
4. OpenAPI EndUser 查询必须校验租户、App 和 EndUser 三者关系。
5. Workspace 文件读写、WebApp 会话文件访问必须使用统一的归属校验。

### P0：Agent 调用一致性

1. 四个聊天入口统一事件模型和持久化流程。
2. 统一运行时资源关闭和异常收尾。
3. 统一停止任务的权限判断和状态事件。
4. 统一非流式响应中的 token、latency、status 和错误字段。
5. SSE 事件统一包含 `task_id`、`conversation_id`、`message_id` 和事件类型。

### P1：配置契约

1. 草稿配置请求不能长期使用无 Schema 的任意 JSON。
2. `skills`、`subagents`、`tools` 的存储格式和展示格式继续分离：
   存储引用，返回前端可展示对象。
3. 模型参数由模型实体定义驱动，前端不能依赖写死的参数列表。
4. 配置读取不应为了展示而无条件修改数据库；清理失效引用需要明确事务边界。
5. 发布配置和草稿配置必须使用同一套规范化规则。

### P1：事件和持久化

1. `AGENT_MESSAGE` 增量事件在一个地方聚合。
2. `TODO_UPDATE`、`SUB_AGENT_DELEGATE`、`SUB_AGENT_END` 保留给前端展示，
   但不进入自定义 State。
3. 事件持久化只保留可审计字段，不把完整提示词、长期记忆或敏感运行上下文
   写入前端响应。
4. 生成会话名称失败不能影响主回答。

## 5. 实施顺序

### 第 1 阶段：不改变 URL 的契约修正

- 修正跨租户查询和删除范围；
- 修正 `task_id` 持久化；
- 修正调试会话清理；
- 把 Handler 中的业务初始化移动到 Service；
- 补齐错误、停止、超时的消息状态。

### 第 2 阶段：抽取统一 Agent Invocation Service

- 提取模型、工具、知识库、工作流和 Runtime 装配；
- 提取 SSE 事件聚合；
- 提取 Agent 轨迹持久化调用；
- 保留现有四类 URL 和响应字段；
- 先让旧 Handler 调用新服务，不先改前端。

### 第 3 阶段：收拢 App 配置职责

- 将 `_validate_draft_app_config` 中的配置规则移入 `AppConfigService`；
- `AppService` 只负责版本、发布和应用生命周期；
- 增加明确的请求 Schema 和响应 Schema；
- 统一草稿和发布配置的字段语义。

### 第 4 阶段：稳定接口文档和前端契约

- 重新生成当前真实接口清单；
- 固定 SSE 事件类型和字段；
- 固定应用配置、Skill、SubAgent、Workspace 文件响应；
- 再开始前端 API service、model、store 和页面改造。

## 6. 暂不处理

- 不新增 `AgentRuntimePlan`；
- 不恢复自定义 `history`、`iteration_count` 或滑动窗口；
- 不把 `opening_statement` 写入 system prompt；
- 不实现第二套长期记忆摘要；
- 不把所有租户写入同一个 `AGENTS.md`；
- 不在前端展示当前未实现的异步 SubAgent、HITL 或 Sandbox 能力；
- 不为了“统一”拆出大量只有一个方法的 Service。

## 7. 后端完成判定

前端适配开始前，后端至少满足：

- `app.http.app` 可以被 Flask 正常加载；
- `flask db` 命令可用；
- 所有 Router 注册的 Handler 都能解析；
- 四类 Agent 调用入口共享同一套 Invocation Service；
- `pip check` 无损坏依赖；
- 关键接口的请求/响应 Schema 已固定；
- 跨租户、跨 App、跨 Conversation 的权限边界已统一；
- SSE 正常结束、错误、停止和超时事件可被前端稳定消费。
