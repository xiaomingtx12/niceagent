# DeepAgents V2 业务流程与数据库改造方案

> 文档类型：业务流程与数据契约。
>
> 适用版本：my-dify 当前工作区，DeepAgents 0.6.12。
>
> 本文区分“当前数据库事实”和“目标改造”。当前完成度以 `02-implementation-progress-and-roadmap.md` 为准；领域行为分别以 `03`、`04`、`05` 为准。表名、字段和迁移在实施前必须再次与模型代码和迁移链核对。

## 1. 业务对象与归属

| 对象 | 业务归属 | 说明 |
|---|---|---|
| Account | 平台租户 | 所有 App、资源和运行数据的第一隔离边界 |
| App | Account | 对外应用和 Creator 配置入口 |
| AppConfig | App | 草稿配置；保存主 Agent、工具、工作流、Skill 绑定和 SubAgent 配置 |
| AppConfigVersion | App | 已发布和历史配置快照 |
| EndUser | Account + App | 终端用户身份；长期记忆必须按该作用域隔离 |
| Conversation | App + 调用主体 | 对话容器，对应稳定 thread 的业务定位信息 |
| Message | Conversation | 原始用户输入和助手回复；用于展示、审计和冷启动 |
| MessageAgentThought | Message | 工具、Todo、委派和 Agent 运行轨迹 |
| WorkspaceFile | Account + App + Conversation | 会话文件、产物和框架历史外置内容的业务存储记录 |
| MemoryFact | Account + App + EndUser | 可管理的长期用户事实 |
| Builtin Skill | 平台 | 部署镜像内公共、版本化、只读内容 |

任何 Handler、Service、Backend、后台任务或 Weaviate 查询都必须从服务端 RuntimeScope 获取作用域，不能信任模型或客户端提供的 account/app/conversation/end_user 标识。

## 2. Creator 业务流程

### 2.1 创建和编辑应用

1. Creator 创建 App，系统创建或关联草稿 `AppConfig`。
2. 配置主 Agent 模型、提示词、业务工具和工作流。
3. 在 Web 平台浏览内置 Skill 目录，并把选中的 Skill 挂载到主 Agent；Creator 不能上传、编辑或覆盖 `SKILL.md` 和脚本。
4. 配置零个或多个同步 SubAgent：角色名、职责、system prompt、模型、工具子集，以及从主 Agent 已挂载集合中选择的 Skill 子集。
5. 草稿调试和正式运行都调用统一 `AgentRuntimeService`，只在配置版本和调用主体上不同。

### 2.2 发布

发布前必须完成以下校验：

- 所有工具和 WorkflowTool 都存在于当前 App 的授权工具注册表。
- SubAgent 工具是主 Agent 工具的子集，Skill 是 App 已绑定 Skill 的子集。
- SubAgent `mode=sync`、`context_passing=NONE`，不存在异步和父历史复制字段。
- Skill 的 name、version、digest 与公共只读目录一致。
- 文件权限最后存在兜底 deny，不允许物理路径和通用 execute。
- 模型配置、Harness Profile 和 checkpointer 配置可构建。

校验通过后创建 `AppConfigVersion` 快照，并让 `App.app_config_id` 指向已发布版本。禁止发布时动态读取 Creator 尚未发布的内容。

### 2.3 Creator 资源体验

Creator 可以：

- 查看平台内置 Skill 的名称、版本、描述、digest、脚本能力和挂载状态，并挂载/卸载到主 Agent 或同步 SubAgent；不能修改正文和脚本。
- 管理 App 发布资源和测试输入。
- 查看 Workspace/Artifacts 的受控文件列表、版本和审计信息。
- 查看同步委派树、Todo 状态、工具调用和失败原因。
- 查看长期记忆诊断信息；默认不能静默改写终端用户事实。

Creator 是否可以删除违规长期事实属于产品权限决策。落地前必须固定为独立权限，例如 `memory_fact:moderate`，不能把“Creator 可查看”解释成自动具有编辑和删除权限。

## 3. 终端用户对话流程

```mermaid
sequenceDiagram
    participant U as 终端用户
    participant API as 调用入口
    participant RT as AgentRuntimeService
    participant A as 主 Agent
    participant S as 同步 SubAgent
    participant DB as PostgreSQL

    U->>API: 发送消息
    API->>DB: 校验 App、Conversation、EndUser
    API->>RT: 创建 RuntimeScope
    RT->>RT: 装配 Backend/permissions/skills/memory/tools/subagents/checkpointer
    RT->>A: create_deep_agent 运行
    A->>A: write_todos 更新计划（可选）
    A->>S: task(自包含说明, 角色)（可选）
    S-->>A: 最终结果
    A->>A: 综合结果并更新 Todo
    A-->>API: DeepAgents/LangGraph 事件
    API->>DB: 保存 Message 与 MessageAgentThought
    API-->>U: SSE/同步回复
    API-->>DB: 对话成功后异步提取候选长期事实
```

运行规则：

1. 使用稳定 `thread_id = account_id:app_id:conversation_id:agent_id`。
2. 有 checkpoint 时恢复短期线程状态；无 checkpoint 时才从原始 Message 冷启动。
3. 长期事实通过只读 `/memories/profile.md` 和 `memory=` 投影，不直接拼接到 system prompt。
4. DeepAgents 原生 Summarization 负责压缩，历史外置到 `/conversation_history/{thread_id}.md`。
5. Workspace 文件是数据，不是执行入口。
6. 所有运行事件经过 `EventTranslationMiddleware` 翻译后再持久化和推流。

## 4. Todo 与 SubAgent 调度职责

Todo 是计划与状态事实，可以驱动委派决策；真正的执行入口是 DeepAgents 原生 `task`：

```text
主 Agent 拆解任务
  -> write_todos 保存计划
  -> 选择依赖已满足的 Todo
  -> 直接调用业务工具，或调用 task 委派同步 SubAgent
  -> 校验返回结果
  -> write_todos 更新 completed / failed / revised
```

强制边界：

- `write_todos` 只修改 Todo state，不直接创建 SubAgent。
- 主 Agent 模型负责 V1 的选择和委派决策，不新增自建 Todo 调度器。
- `task` 调用必须重新经过角色、工具、Skill、文件和租户权限校验。
- Todo 中的文本不能声明新工具、物理路径、命令或更高权限。
- Todo 和 SubAgent 运行状态由 checkpoint 保存；不新增 `plans`、`todos` 或 `subagent_tasks` 业务表。
- `TODO_UPDATE` 从 DeepAgents Todo state 变化翻译为业务事件，并按现有 Thought 机制持久化。

以后若产品需要跨请求的确定性调度、重试或异步恢复，应新增独立调度层，但仍不得把执行副作用塞进 `write_todos`。

## 5. 父子 Agent 工具和工作流选择

### 5.1 统一工具注册表

主 Agent 和 SubAgent 都只能从同一个服务端工具注册表解析能力：

| 工具类型 | 主 Agent | SubAgent |
|---|---|---|
| BaseTool | AppConfig 已绑定且调用主体有权使用 | `subagents[].tools` 显式引用的父工具子集 |
| WorkflowTool | 与 BaseTool 相同，注册为具名工具 | 只有通过 SubAgent 安全校验后才能进入子集 |
| DeepAgents `task` | 有显式团队时由原生中间件提供 | 禁止，保持单层拓扑 |
| 文件工具 | DeepAgents 原生提供，受 permissions 和 Backend ACL 限制 | V1 默认只读且只见授权目录 |
| `run_builtin_skill_script` | 仅绑定可执行内置 Skill 且生产沙箱可用时提供 | 必须另外显式授权，不从 Skill 自动获得 |
| 通用 `execute` | 线上禁止 | 线上禁止 |

### 5.2 WorkflowTool 嵌套权限

WorkflowTool 可以被 SubAgent 调用，但必须满足：

1. 工作流已经绑定到当前 AppConfig 版本。
2. SubAgent 的 `tools` 显式包含该工作流工具名。
3. 发布阶段生成工作流能力清单，至少标记读文件、写文件、联网、代码执行、外部副作用和敏感数据访问。
4. 工作流每个节点在执行时重新进行权限门控；父工具通过不代表内部节点自动通过。
5. 工作流节点的能力并集不能超过主 Agent 和当前调用主体的授权范围。
6. 包含任意代码、动态命令、未声明外部调用或不可信定义加载的工作流，V1 不得授权给线上 SubAgent。

## 6. AppConfig 数据契约

### 6.1 同步 SubAgent

`app_config.subagents` 和 `app_config_version.subagents` 是 JSONB 数组，配置随发布版本固化：

```json
[
  {
    "name": "researcher",
    "description": "搜索并归纳资料",
    "system_prompt": "你是严谨的研究员。",
    "mode": "sync",
    "context_passing": "NONE",
    "tools": ["web_search", "workflow:collect_sources"],
    "skills": ["research"],
    "model": "openai:gpt-5.4"
  }
]
```

只允许以下字段：

| 字段 | 规则 |
|---|---|
| `name` | App 内唯一稳定路由键，小写字母开头，最多 64 位 |
| `description` | 给主 Agent 的角色说明和委派场景 |
| `system_prompt` | SubAgent 角色约束，不包含父历史 |
| `mode` | V1 只接受 `sync` |
| `context_passing` | V1 只接受 `NONE` |
| `tools` | 统一工具注册表中的父工具子集；空数组表示无业务工具 |
| `skills` | 当前 App 已绑定内置 Skill 的子集 |
| `model` | 可选 DeepAgents 模型覆盖；省略时使用主模型 |

`graph_id`、`url`、`headers`、`k`、`timeout_seconds`、`max_concurrent_per_conversation` 及其他异步字段必须拒绝，不能忽略。

### 6.2 内置 Skill 绑定

目标是在 `AppConfig` 和 `AppConfigVersion` 中保存绑定引用，不保存正文：

```json
[
  {
    "name": "csv-analysis",
    "version": "1.0.0",
    "digest": "sha256:...",
    "enabled": true
  }
]
```

该数组表示主 Agent 的挂载集合。SubAgent 只能在 `subagents[].skills` 中按名称引用这个集合的子集。公共目录中的 Skill 包是正文权威来源；数据库不得保存同一 Skill 的第二份权威正文。

Web/API 契约固定为：

1. `GET /apps/{app_id}/skills` 从 `BuiltinSkillCatalog` 返回可见内置目录元数据和当前草稿挂载状态，不返回可编辑正文。
2. Skill 挂载通过现有 AppConfig 草稿保存/发布流程写入 `app_config.skills`，不通过创建 Skill 内容的 POST 接口完成。
3. SubAgent 挂载通过 `subagents[].skills` 保存，后端校验它是主 Agent 挂载集合的子集。
4. 发布后的运行严格使用 `AppConfigVersion` 中固化的 name、version 和 digest；目录版本或 digest 不匹配时拒绝构建 Agent。
5. Web 平台不提供新增、编辑正文、上传包或删除内置 Skill 的入口。

## 7. 当前相关数据库表

以下是当前模型代码中的真实表名，不代表所有目标字段已经完成：

| 表 | 当前用途 | 本轮处理 |
|---|---|---|
| `app` | App 与草稿/发布配置引用 | 复用 |
| `app_config` | 草稿模型、工具、工作流、SubAgent 等配置 | 复用并补齐 Skill 绑定 |
| `app_config_version` | 发布和历史配置快照 | 复用并补齐 Skill 绑定 |
| `conversation` | 会话元数据 | 复用 |
| `message` | 原始问答、token、价格、状态 | 复用 |
| `message_agent_thought` | 现有 Agent 工具和运行轨迹 | 复用并增加多 Agent 追踪字段；表名是单数 |
| `end_user` | App 范围终端用户 | 复用并固定外部用户唯一性 |
| `workspace_files` | 版本化 Workspace 文件 | 复用并完成命名空间、配额和 ACL |
| `memory_facts` | 长期事实 | 复用并移除向量字段、补齐治理字段 |
| `skills` | 当前开发中的数据库 Skill 正文 | 不进入 V1；移除内容 CRUD 和未发布迁移 |

LangGraph checkpoint 使用框架 saver 管理，不新增 my-dify 自定义 `agent_checkpoints` 模型。

## 8. 目标字段调整

### 8.1 `message_agent_thought`

这是现有表，后续通过 `ALTER TABLE` 增加字段，不创建复数新表：

| 字段 | 类型建议 | 语义 |
|---|---|---|
| `agent_id` | String(64), nullable | 当前产生事件的 Agent 路由键；主 Agent 使用稳定值 `main` |
| `agent_type` | String(16), nullable | `main` 或 `subagent` |
| `parent_agent_id` | String(64), nullable | SubAgent 的父 Agent；主 Agent为空 |
| `delegation_id` | String(128), nullable | 同步委派使用 DeepAgents tool call id，关联 delegate/end |
| `task_id` | String(128), nullable | 预留后台异步任务 ID；同步阶段为空 |

同步阶段不能把 `task_id` 当作 DeepAgents tool call id。前端按 `delegation_id` 关联委派树。

推荐索引：

- `(message_id, position)`：沿用现有顺序访问。
- `(conversation_id, delegation_id)`：定位一次同步委派。
- `(conversation_id, agent_id, created_at)`：按角色展示轨迹。
- `task_id` 非空时的部分索引：仅在异步阶段真正启用后增加。

### 8.2 `memory_facts`

目标表不保存 embedding。核心字段至少包括：

| 字段 | 作用 |
|---|---|
| `id`, `account_id`, `app_id`, `end_user_id` | 主键与租户/用户作用域 |
| `fact_type`, `content`, `importance` | 事实内容与召回优先级 |
| `normalized_key` | 同一作用域的去重和幂等更新 |
| `status` | `pending / active / rejected / expired` |
| `source_type`, `source_conversation_id`, `source_message_id` | 来源审计 |
| `created_by` | 用户、系统任务或管理员来源 |
| `expires_at`, `deleted_at` | 过期和软删除 |
| `created_at`, `updated_at` | 生命周期 |

约束和索引至少包括：

- `(account_id, app_id, end_user_id, normalized_key)` 的作用域唯一性策略。
- 可见事实查询索引覆盖 scope、status、deleted_at、expires_at、importance、updated_at。
- source 字段支持自动提取幂等。
- 删除、过期和拒绝事实不能进入 `/memories/profile.md`。

Weaviate 仅可保存可重建候选索引和 `fact_id`；查询后必须回查 PostgreSQL。不得添加 pgvector 扩展、向量列或自定义余弦检索。

### 8.3 `workspace_files`

继续使用当前版本化表，服务层必须保证：

- `account_id + app_id + conversation_id + path + version` 的作用域和版本一致性。
- 版本分配使用事务锁、唯一约束或原子序列，不能依赖无锁 `latest + 1`。
- 逻辑路径只允许 `/workspace`、`/artifacts` 等已注册命名空间。
- 内容、文件数、扫描量、结果数和写入频率有配额。
- 物理路径、存储 key 和租户 ID 不进入模型上下文。
- Workspace 文件不可作为命令、代码模块、Skill、Hook 或工作流定义执行。

### 8.4 `skills`

当前 `skills` 表包含正文并提供 Creator CRUD，但它不是 V1 最终权威源。整改规则：

1. 运行时停止从该表读取 Skill 正文。
2. Creator UI 和 `GET /apps/{app_id}/skills` 改为读取 `BuiltinSkillCatalog`，只提供查看和挂载状态。
3. 移除 Creator Skill 正文的 create/edit/enable/delete API、Service、模型和前端编辑器。
4. `d_v2_005_create_skills` 尚未进入共享生产基线，因此从 V1 迁移链删除，并把后续 revision 的 `down_revision` 接到前一个有效 revision。
5. Skill 绑定直接随 AppConfig/AppConfigVersion 版本化，不另建绑定表。

以后如开放平台管理员上传包，必须设计独立的签名、扫描、审核和发布仓库；不能恢复当前租户可编辑正文表作为执行来源。

## 9. 当前迁移链与整改方式

当前工作区迁移链为：

| Revision | 当前内容 | 处理结论 |
|---|---|---|
| `d_v2_001_drop_agent_checkpoints` | 删除旧自建 checkpoint 表 | 保留；使用 LangGraph saver |
| `d_v2_002_create_workspace_files` | 创建 `workspace_files` | 保留并验证升降级 |
| `d_v2_003_create_memory_facts` | 创建 `memory_facts` | 保留，字段由后续迁移补齐 |
| `d_v2_004_add_subagents` | 给 AppConfig/Version 增加 `subagents` | 保留 |
| `d_v2_005_create_skills` | 创建数据库 `skills` 表 | 未发布旧实现；V1 删除该 revision 并重接迁移链 |
| `d_v2_006_finalize_memory` | 当前包含 pgvector、embedding、来源字段和 EndUser 调整 | 合并前必须移除 pgvector/embedding，并补齐最终治理字段 |
| `d_v2_007_subagents_array` | 把 SubAgent 配置规范化为数组 | 保留，确保只接受同步 schema |

这些 revision 当前属于未完成开发链。执行规则：

1. 在进入共享生产基线前，修正 `d_v2_006`，不把已否决的 vector 扩展和 embedding 列带入发布迁移。
2. 已在个人开发数据库执行旧版 `d_v2_006` 时，使用可重复的开发清理步骤或重建开发库；不能据此保留错误生产设计。
3. 已进入共享或生产环境的迁移不得改写历史，必须新增向前清理 revision。
4. 删除未发布的 `d_v2_005_create_skills` 后，调整 `d_v2_006` 的 `down_revision`；最终 revision ID 必须连续、唯一且没有悬空依赖。
5. 多 Agent 轨迹字段和 AppConfig Skill 绑定在实现时使用当前有效链的下一个连续 revision；不要预先跳号或创建空迁移。
6. 每次迁移必须验证 PostgreSQL upgrade、downgrade、索引、约束和存量 JSON 兼容。

## 10. 不新增的表和字段

当前方案明确不新增：

- `agents`、`subagents`、`agent_members`：角色配置随 AppConfig 版本化。
- `plans`、`todos`：Todo state 在 checkpoint 中。
- `subagent_tasks`：同步阶段使用 Thought 轨迹和 delegation ID。
- 自建 `agent_checkpoints`：使用 LangGraph saver。
- 单独的 conversation summary 权威表：摘要属于 checkpoint/压缩状态，原始 Message 保持完整。
- `memory_facts.embedding` 或 pgvector 相关表。
- `skill_scripts`：V1 内置脚本来自受信只读 Skill 包清单。

异步阶段若原生框架仍缺少业务任务注册，是否新增异步任务表必须单独设计，不能提前复用同步 `task_id` 字段冒充完整生命周期。

## 11. 删除与清理语义

| 操作 | 必须处理的数据 |
|---|---|
| 删除 Conversation | 原始消息、Thought、对应 checkpoint、会话 Workspace/Artifacts、conversation history；长期事实按产品策略单独判断 |
| 删除 EndUser | 当前 App 范围的长期事实和可选 Weaviate 投影；保留合规审计所需记录 |
| 删除 App | AppConfig/Version、绑定、会话、Workspace、长期事实和索引投影按异步清理流程处理 |
| 删除 MemoryFact | PostgreSQL 先软删除，再异步删除索引投影；读取立即不可见 |
| 解绑 Skill | 新运行立即不可见；历史 AppConfigVersion 仍保留引用和 digest 供审计 |

删除流程必须幂等，并记录失败重试和跨存储漂移。不能先删 PostgreSQL 权威事实再依赖已经丢失的作用域信息清理外部索引。

## 12. 数据层实施顺序

1. 修正 `d_v2_006`，删除 pgvector/embedding，并把 `memory_facts` 对齐最终治理字段。
2. 给 AppConfig 和 AppConfigVersion 增加或确认内置 Skill 绑定快照，更新序列化、发布和回滚。
3. 删除数据库 Skill 正文模型、CRUD、未发布迁移和前端编辑器；Creator 改为浏览并挂载公共目录条目。
4. 给现有 `message_agent_thought` 增加同步委派追踪字段和索引。
5. 统一 TODO_UPDATE、SUB_AGENT_DELEGATE、SUB_AGENT_END 的持久化字段和前端分组。
6. 完成 Workspace 作用域、版本、配额、审计和不可信内容约束。
7. 验证所有运行入口、后台提取和删除任务的租户过滤。

每一步都必须先更新模型和迁移，再更新 Service/API/UI，最后补自动化测试；不能只改 JSON schema 或只改前端表单。

## 13. 数据层验收门禁

- 模型表名、Alembic revision、Service 查询和文档一致。
- PostgreSQL 全链 upgrade/downgrade 通过，且没有 vector 扩展依赖。
- AppConfig 草稿、发布、历史回滚后，SubAgent 和 Skill 绑定完全一致。
- 未配置团队时不出现 `task`；非法同步字段在 API 层明确拒绝。
- Todo state 可恢复，TODO_UPDATE 与最终状态一致，且没有第二份 plans/todos 数据。
- 同步委派可通过 `delegation_id` 关联开始、结束和角色轨迹。
- MemoryFact 的用户隔离、幂等、状态、过期、软删除和 profile 投影通过。
- Workspace 路径穿越、跨租户、并发版本、配额和不可执行约束通过。
- Skill 正文只能来自公共只读目录，数据库不能覆盖它。
- WorkflowTool 和其内部节点均不能扩大父 Agent 或 SubAgent 权限。
