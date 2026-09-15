# Agent 会话与执行表定案

日期：2026-09-15。依据：[最小交互业务约定](Agent-最小交互业务约定.md)。本方案取代之前尚未定案的改名、合表讨论。ORM 与读写代码已调整，数据库迁移和数据回填尚未执行。

## 1. 结构与职责

保留五张表，不新增单条聊天消息表，不合并问答与执行表。

| 表 / ORM 类 | 职责 | 关系 |
| --- | --- | --- |
| `conversation` / `Conversation` | 持续交流的归属和列表信息 | 一对多 ConversationTurn。 |
| `conversation_turn` / `ConversationTurn` | 一轮原始输入、回答与累计用量 | 一轮最多一个 AgentExecution；历史和微信兼容记录可无 Execution。 |
| `agent_execution` / `AgentExecution` | 本轮逻辑执行控制、表单及恢复信息 | `turn_id` 唯一；表单恢复沿用同一 Execution。 |
| `agent_execution_submission` / `AgentExecutionSubmission` | 一批已接受的补充答案与回执 | `(execution_id, input_batch_id)` 联合主键。 |
| `agent_execution_event` / `AgentExecutionEvent` | 执行过程和续传 | `(execution_id, sequence)` 联合主键。 |

Turn 与 Execution 是同一轮业务的两部分物理记录。保留拆分是为了分别处理历史/统计读取和执行锁/恢复数据，并兼容没有执行记录的历史入口。调用方通过统一开始方法创建本轮，不分别拼装两张表。

Execution 是一次可暂停恢复的逻辑执行，不是后台线程或模型调用。内存执行片段使用 `AgentExecutionSegment`，每次恢复更换 `executor_id`。协议和 SDK 的 Run 与这里的 Execution 对应，页面“任务”可能指整个 Conversation，不改成 AgentTask。

## 2. 字段归属

### Conversation

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | UUID | 会话身份。 |
| `app_id` | UUID | 应用身份；内置助手使用固定标识，无统一 App 外键。 |
| `name` | VARCHAR(255) | 会话标题。 |
| `is_pinned`、`is_deleted` | BOOLEAN | 置顶与软删除。 |
| `invoke_from` | VARCHAR(32)，必填 | 首次创建入口，不使用空字符串作为默认来源。 |
| `created_by` | UUID，可空 | 创建者身份，由固定入口解释 Account/EndUser；不复制拥有者含义。 |
| `owner_account_id` | UUID，可空 | 可管理会话的账号，不等同于应用所有者。 |
| `created_at`、`updated_at` | TIMESTAMP | 创建与修改时间；更新时间由 ORM 更新表达式维护。 |

### ConversationTurn

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | UUID | 本轮身份。 |
| `app_id` | UUID | 应用统计的直接过滤列，创建后不修改。 |
| `conversation_id` | UUID，外键 | 所属会话；物理删除会话时 RESTRICT，软删除不自动清理历史。 |
| `invoke_from` | VARCHAR(32)，必填 | 本轮入口，可以不同于会话创建入口。 |
| `created_by` | UUID | 本轮发送者，由本轮入口解释。 |
| `query`、`answer` | TEXT | 原始问题与已保存回答；表单答案不覆盖 query。 |
| `image_urls`、`attachments` | JSONB 数组 | 外部图片输入、上传附件公开元数据，两种来源暂不强行合并。 |
| `latency` | FLOAT，默认 0 | 累计片段耗时，单位秒，不含等待填写。 |
| `status` | VARCHAR(16)，默认 normal | normal/stop/error 展示结果，兼容无 Execution 的入口，不是运行状态机。 |
| `error` | TEXT | 无 Execution 的历史/渠道错误及展示兼容；工作台以 Execution 错误为主。 |
| `is_deleted` | BOOLEAN | 本轮软删除。 |
| `total_token_count`、`input_token_count`、`output_token_count` | INTEGER，默认 0 | 累计用量，供应商总量缺失时按归一化规则处理。 |
| `cached_token_count`、`reasoning_token_count` | INTEGER，默认 0 | 用量明细，不额外加到总 Token。 |
| `model_call_count`、`tool_call_count` | INTEGER，默认 0 | 调用次数，不能从 Token 推导。 |
| `token_usage` | JSONB 对象 | 按模型的动态明细；固定汇总列供查询和统计直接读取。 |
| `trace_id` | VARCHAR(64)，可空 | 追踪标识；工作台关联 Execution，微信可使用独立追踪 ID。 |
| `total_price` | NUMERIC(10,7) | 历史金额兼容；当前无完整计价，0 不应解释为已确认免费。 |
| `created_at`、`updated_at` | TIMESTAMP | 首次创建与修改时间，暂停恢复不改变 created_at。 |

Model 负责字段映射、索引、主外键及必要唯一性，不再声明状态枚举、JSON 结构、计数范围或跨字段状态规则的 CheckConstraint。输入和领域校验沿用 DTO、Entity 与 Service；数据库不再用 CHECK 拦截绕过这些入口的写入。

### AgentExecution

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | UUID | 逻辑执行身份，对外仍映射 run_id。 |
| `turn_id` | UUID，唯一外键 | 本轮问答；物理删除 Turn 时级联执行记录。 |
| `conversation_id` | UUID，外键 | 会话占用唯一约束和执行查询所需，不从客户端任意修改。 |
| `app_id`、`invoke_from` | UUID / VARCHAR(32) | 归属和入口过滤，必须与 Turn 一致。 |
| `input_message_id` | VARCHAR(255)，可空 | 客户端输入消息标识及启动幂等键，不是 Turn 外键。 |
| `status` | VARCHAR(32) | running/waiting_input/completed/failed/cancelled。 |
| `error_message` | TEXT | 执行失败原因，面向用户，不存异常堆栈。 |
| `execution_options` | JSONB 对象 | 下述七项可恢复选项，不复制身份和原始内容。 |
| `pending_inputs` | JSONB 数组 | 当前待填批次，提交或结束后清空。 |
| `input_history` | JSONB 数组 | 已确认的问题及答案，用于展示和后续调用。 |
| `display_state` | JSONB 对象 | 当前轮消息及 todos/files/async_tasks 展示白名单。 |
| `last_event_sequence` | INTEGER | 已提交续传水位；仍遵守会话与入口内跨执行递增规则，不是事件条数。 |
| `executor_id` | UUID，可空 | 当前执行片段写入资格；仅 running 非空，结算撤销。 |
| `cancel_requested_at` | TIMESTAMP，可空 | 首次取消请求；与真实状态组合显示“正在停止”。 |
| `finished_at` | TIMESTAMP，可空 | 终态时间，等待填写为空。 |
| `created_at` | TIMESTAMP | 首次启动时间，恢复不刷新。 |

`execution_options` 通过 `AgentExecutionOptions` 校验，只保存：

```text
planning_enabled
selected_skills
user_input_enabled
definition_fingerprint
reasoning_effort
model_selection
model_selection_explicit
```

恢复时读取不可变 Turn 输入，按附件 ID 重新授权并获取稳定存储键，再结合当前授权身份和上述选项组装内存 `AgentInvocation`。上传文件已不可用时返回恢复错误，不能使用失效快照绕过授权。选择来源标志保留以兼容旧客户端自动回传默认模型的历史语义。

### Submission 与 Event

Submission：`execution_id UUID`、`input_batch_id VARCHAR(64)`、`answers JSONB 对象`、`accepted_sequence INTEGER`、`created_at TIMESTAMP`。接受游标非负，不强制关联具体事件行。

Event：`execution_id UUID`、`sequence INTEGER`、`payload JSONB 对象`、`created_at TIMESTAMP`。序号为正，载荷显式提供，不再默认写入无效空对象。两表对 Execution 的物理删除使用 CASCADE，软删除不会触发它。

## 3. 去重与保留冗余的决定

- 删除笼统 `interaction_state`，保留直接读写的待填和历史列；删除取消重开状态。
- `invocation_snapshot` 收敛为 `execution_options`：删除重复的账号、应用、会话、本轮 ID、query、图片、附件和客户端消息 ID。
- `state_snapshot` 改为 `display_state`，只保存展示白名单和当前轮消息；片段结算后清除临时消息投影，回答从 Turn 读取，执行内部状态由 checkpoint 保存。
- 删除没有调用者且将“零轮/一轮”混为新会话的 `Conversation.is_new`；首轮判断继续由创建服务查询未删除问答。
- 去掉来源和展示状态的空字符串默认值，修正仅声明 `server_onupdate` 却不会自动更新时间的问题，使用 ORM `onupdate`。
- 保留 app_id 和 Execution.conversation_id：分别服务于直接统计/过滤和会话占用唯一约束。普通外键只保证存在，归属一致性由创建服务负责。
- 保留 Token 汇总与模型明细，由同一个累计方法更新；不在 Execution 再保存同一份用量。
- 用户确认会话摘要已退出业务，删除 `summary` 及前后端搜索、响应和类型引用，会话检索使用名称。
- 保留 total_price、trace_id 和兼容状态/错误：有已有读者或历史信息，不在结构改名时删除产品功能。后续退出需单独同步消费者。
- 暂不统一成 TIMESTAMPTZ：历史本地时间与 UTC 无时区值需要先核实，不能靠改列类型猜测原始时区。

## 4. 协议兼容

数据库和 ORM 使用新命名，外部协议继续沿用 SDK 的 `run_id`、现有历史响应的 `message_id/business_message_id`。这些键由边界映射到 Execution.id/Turn.id，历史事件和测评材料不批量重写。框架 HumanMessage/AIMessage、事件通道 messages、客户端 input_message_id 也保持原义。

附属引用：`wechat_message.message_id → turn_id`；`agent_evaluation.run_id → execution_id`，测评对外 run_id 通过 Schema 映射。测评仍保留材料快照，不因本次表改名增加删除联动。

内部创建与分页命名使用 `create_agent_turn/get_turn_page`，DTO 使用 `ConversationTurnResp`。协议编排服务文件及命令仍使用 Run 词汇，不通过全局字符串替换破坏 SDK 契约。

## 5. 维护者迁移顺序

禁止将改名误执行成 drop/create。以下仅是映射说明，不是已生成或已执行的迁移。

1. 停止旧版本写入、等待活动执行退出并备份；检查孤立关联、非法来源、非法状态/JSON、负计数。无法确定的历史记录先核实，不能靠新默认值掩盖。
2. 保留主键原值，将 message、agent_run、agent_run_submission、agent_run_event 分别重命名为目标四表。conversation 不改名。
3. 重命名 Execution.message_id 为 turn_id，Submission/Event.run_id 为 execution_id；同步微信与测评关联字段、外键、唯一约束和索引。
4. 若还存在 interaction_state，按[首批字段调整](Agent-Run表单字段调整.md)迁入 pending_inputs/input_history 后再删除旧列。此前已完成该迁移则跳过，不重复累计历史。
5. invocation_snapshot 改为 execution_options 并按七项白名单提取；迁移前核对其原始输入与 Turn 一致、Turn 附件 ID 有有效上传记录。差异应保留并核实，不能丢弃唯一输入来源。旧记录缺少选项时按 DTO 默认值处理，模型选择来源保留原语义。
6. state_snapshot 改为 display_state；按展示白名单处理旧键。保持事件 payload、序号、Submission 回执和 checkpoint 身份不变。
7. 完成 VARCHAR 收窄、默认值和外键变更；移除旧业务 CHECK，不新增对应 CHECK。自动迁移未必能检测所有 CHECK 删除，维护者应单独核对。空状态不能一律伪造为成功，应按旧执行和渠道结果核实。新 onupdate 为 ORM 行为，不代表新增数据库触发器。
   同时删除用户已确认弃用的 `conversation.summary`；应用代码和前端不再读写该字段。
8. 核对记录数量、主键、关系、待填批次、用量和金额后，部署配套版本并按最小交互场景验收。新 ORM 无法直接运行在尚未改名/迁列的数据库上。

本次不编写或执行 revision，不改迁移链。真实落库和功能联调由维护者执行。
