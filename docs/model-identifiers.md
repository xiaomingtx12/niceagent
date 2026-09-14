# Model ID 字段说明

核对日期：2026-09-14。对应 `internal/model` 当前代码；业务流程见 [Agent 业务说明](agent-business.md)。本次结构调整限于 EndUser，其余内容说明现有含义。

## 1. 先分清谁拥有应用、谁使用应用

假设账号 A 创建一个应用，账号 B 登录后使用它：

```text
App.id = X
App.account_id = A              应用所有者

EndUser.id = E                  B 在应用 X 中的用户记录
EndUser.app_id = X
EndUser.account_id = B          应用使用者的登录账号

Conversation.app_id = X
Conversation.created_by = E     WebApp 会话的创建来源
Conversation.owner_account_id = B   会话管理归属

Message.created_by = E          本轮通过 WebApp 发送的消息来源
```

应用所有者与会话归属账号是两个角色。同一个账号也可以同时担任两个角色，但不能因此认为这两个概念相同。

### EndUser 的最终字段

| 字段 | 引用／含义 | 可空 |
| --- | --- | --- |
| `id` | EndUser 自己的主键，协议中的 `end_user_id` 指向它 | 否 |
| `app_id` | 使用的 App；用户身份只在该应用内有效 | 否 |
| `account_id` | 使用者关联的 Account，不是应用所有者 | 是 |

同一个 Account 使用不同 App，会有不同 EndUser。唯一索引 `(app_id, account_id)` 保证同账号在同应用只有一条映射。PostgreSQL 默认允许多条 `account_id IS NULL` 的记录，OpenAPI／微信的不同未绑定用户不会因此冲突。

原 `external_user_id` 改名为 `account_id`；原 `tenant_id` 删除。原 tenant_id 保存的应用所有者可以通过 `EndUser.app_id → App.account_id` 获取，不再维护一份副本。微信 openid 仍由 WechatEndUser 保存，不写入 account_id，也不把应用所有者自动当作未登录消费者。

## 2. 鉴权如何使用这些 ID

| 入口 | EndUser 从哪里来 | 授权依据 |
| --- | --- | --- |
| WebApp | 服务端用 `(app_id, current_user.id)` 查询／并发创建 EndUser，写入 account_id | 先确认已发布 WebApp，再检查会话 owner_account_id；旧会话有来源映射兜底 |
| OpenAPI | 创建会话时可以传 EndUser.id，不传就创建未绑定账号的 EndUser | 先确认当前认证账号拥有已发布 App，再确认 EndUser.app_id 匹配；后续从已授权会话读取来源 |
| 微信 | 由回调入口确定发送方，按 `(openid, app_id)` 查 WechatEndUser.end_user_id | 由渠道入口处理回调及应用校验，不凭 EndUser UUID 登录 |
| 调试／创建者任务／助手 | 通常直接使用 Account，不额外创建 EndUser | 对应入口的账号、应用及会话授权 |

OpenAPI 的 Account 表示集成方的应用所有者，该凭证可以操作其获准访问的 Service API 会话；当前没有向每个 EndUser 签发独立凭证。接入方如果直接面向自己的多个用户，需要由接入方服务端验证消费者与会话的关系，不能把 end_user_id 当作密码或终端用户级授权凭证。

## 3. 会话、消息与任务

| Model.字段 | 实际含义 | 容易混淆之处 |
| --- | --- | --- |
| `Conversation.id` | 业务会话主键；框架 thread/checkpoint 使用对应会话标识 | 不是 Run.id，也不是 SSE 连接 ID |
| `Conversation.app_id` | 会话所属应用标识 | 内置助手使用固定标识，不要求有 App 行 |
| `Conversation.owner_account_id` | 可管理会话的 Account.id | 不是默认等于 App.account_id；未回填历史记录或渠道会话可为空 |
| `Conversation.created_by` | 首次创建来源：Account.id 或 EndUser.id | 当前仍是混合类型字段，不能单独当作统一授权主体 |
| `Message.id` | 一轮问答及累计用量的主键 | 不是一个 token 片段或一条框架消息 |
| `Message.app_id / conversation_id` | 本轮所属应用与会话 | 归属需成组校验 |
| `Message.created_by` | 本轮入口的发送来源：Account.id 或 EndUser.id | 同一会话换入口后，可以与会话 created_by 不同 |
| `Message.trace_id` | 观测关联标识；工作台通常为 Run.id 的字符串 | 微信兼容链路使用独立 trace，不能对它强加 Run 外键 |
| `AgentRun.id` | 一次用户任务的主键 | 暂停／恢复不换；新任务才换 |
| `AgentRun.app_id / conversation_id / message_id` | 任务所属应用、会话及本轮 Message | 一条 Message 最多一个工作台 Run，兼容消息可没有 Run |
| `AgentRun.input_message_id` | 客户端输入消息标识，用于即时显示对齐和重复启动判断 | 可为空、可为任意协议允许的字符串，不是 Message.id 外键 |
| `AgentRun.executor_id` | 当前一次执行的租约标识 | 启动／恢复换新，结算清空；不是 Account、进程 ID 或永久任务身份 |
| `AgentRunSubmission.run_id` | AgentRun.id | 与 input_batch_id 共同构成主键 |
| `AgentRunSubmission.input_batch_id` | 整批中断 ID 集合的摘要 | 不包含答案值，不是用户身份 |
| `AgentRunEvent.run_id + sequence` | Run 内持久化事件的联合主键 | sequence 是允许跳号的游标，不是记录数量 |
| `AgentEvaluation.id` | 独立测评身份 | 与来源 Run.id 不同，可多次测评同一任务 |
| `AgentEvaluation.account_id / app_id / run_id` | 测评发起账号／被测应用／来源 Run | 来源材料会冻结，评估执行不会替换原 Run |

`created_by` 的正常写入类型：调试、创建者任务、助手为 Account；WebApp、Service API（含微信兼容消息）为 EndUser。旧记录的归属判断保留现有兼容分支，不能直接按 UUID 外形推断类型。此次不拆分字段，不回填历史数据。

## 4. 其他 Model 的 ID 对照

以下“引用”描述逻辑关系，不表示数据库已建立物理外键。各表 `id` 默认表示本表记录主键；Event、Submission 使用前述联合主键。

| Model | ID 字段及引用对象 |
| --- | --- |
| `Account` | `id`：平台账号 |
| `AccountOAuth` | `account_id → Account.id`；`openid`：provider 作用域的第三方身份，不是 EndUser.id |
| `App` | `account_id → 应用所有者 Account.id`；`app_config_id → 当前发布 AppConfig.id`；`draft_app_config_id → 草稿 AppConfigVersion.id` |
| `AppConfig / AppConfigVersion` | `app_id → App.id`；版本表同时保存草稿与发布历史，版本号不等于记录主键 |
| `AppDatasetJoin` | `app_id → App.id`；`dataset_id → Dataset.id` |
| `ApiKey` | `account_id → 持有平台 API 凭证的 Account.id`；记录 id 不等于 api_key 凭证内容 |
| `ProviderApiKey` | `account_id → 模型连接所有者 Account.id`；本表 id 是配置中的 provider_connection_id，不是提供商名称或密钥内容 |
| `ApiToolProvider / ApiTool` | `account_id → 资源所属 Account.id`；`ApiTool.provider_id → ApiToolProvider.id`，不是 ProviderApiKey.id |
| `MCPServer / MCPTool` | `account_id → 资源所属 Account.id`；`MCPTool.server_id → MCPServer.id` |
| `Dataset` | `account_id → 知识库所属 Account.id`；`embedding_provider_connection_id → ProviderApiKey.id`，可为空 |
| `Document` | `account_id → 所属 Account.id`；`dataset_id → Dataset.id`；`upload_file_id → UploadFile.id`；`process_rule_id → ProcessRule.id` |
| `Segment` | `account_id → 所属 Account.id`；`dataset_id → Dataset.id`；`document_id → Document.id`；`node_id` 是向量存储中的对象身份，与本表 id 分开 |
| `KeywordTable` | `dataset_id → Dataset.id` |
| `DatasetQuery` | `dataset_id → Dataset.id`；`source_app_id` 为检索来源应用，可空；当前服务将检索 Account.id 写入 created_by |
| `ProcessRule` | `account_id → 所属 Account.id`；`dataset_id → Dataset.id` |
| `UploadFile` | `account_id → 上传文件所属 Account.id`；对象存储 key 不等于本表 id |
| `Workflow / WorkflowResult` | `account_id → 创建／执行记录所属 Account.id`；结果的 `workflow_id → Workflow.id`，`app_id` 为来源应用，可空 |
| `WechatConfig` | `app_id → 平台 App.id`；`wechat_app_id` 为微信公众号的开发者标识 |
| `WechatEndUser` | `app_id → 平台 App.id`；`end_user_id → EndUser.id`；`openid` 是当前公众号发送方身份 |
| `WechatMessage` | `wechat_end_user_id → WechatEndUser.id`；`message_id → Message.id` |
| `WorkspaceFile` | `account_id + app_id` 定义共享账号／应用范围；conversation_id 的特殊含义见下文 |

### Workspace 的历史命名例外

`WorkspaceFile.conversation_id` 当前由仓储写入账号与应用派生的稳定 `scope_id`，不一定对应 Conversation 表的任何记录；文件因此可以跨会话共享。不能按字段名称给它添加 Conversation 外键。

`WorkspaceFile.created_by` 当前可能是 Account.id 或 EndUser.id；后续有来源的写入会更新它，因此它也不是不可变的最初创建者。鉴权使用可信账号与应用作用域，不使用这个字段。当前只明确注释，物理列名及文件路径格式不在本次结构调整范围内。

## 5. EndUser 切换要求

1. 将旧列 `external_user_id` **保留原值重命名**为 `account_id`。不能从 tenant_id 回填，否则会把应用所有者误当成使用者。
2. 对齐唯一索引名 `uq_end_user_app_account`，列为 `(app_id, account_id)`；保留 app_id 查询索引，去掉 tenant_id 索引与列。
3. 删除旧列前核对 `end_user.app_id` 是否指向存在的 App，旧 tenant_id 是否与 App.account_id 一致；不一致的数据先确认来源，不静默扩大访问范围。
4. 保留 EndUser.id、app_id 以及会话／微信对 EndUser.id 的引用。OpenAPI 与微信已有空账号映射继续为空；不额外补造平台账号。
5. 核对重命名后 `(app_id, account_id)` 非空组合无重复。账号不同或应用不同的记录保持隔离。
6. 新代码不再使用旧字段；旧数据库未经切换会与 ORM 不兼容。自动生成工具可能把重命名识别为删列加列，需在用户生成后审核为保值重命名。

按项目规范，本次没有生成或编辑迁移、连接业务数据库或回填数据。原迁移与历史 ER 图仍描述旧列，不能用来判断当前 Model 的含义。

## 6. 核验范围

需要验证：WebApp 同账号同应用复用映射、不同账号／应用隔离、首次创建唯一冲突后重新读取、OpenAPI 拒绝跨应用 EndUser 和非应用所有者、未绑定账号的多个消费主体可以共存、旧会话归属仍使用 EndUser.account_id 映射。

2026-09-14 本地验证通过：Model 与相关服务 Python 语法、ORM 映射、全部 Model 的 PostgreSQL 建表和索引编译、文档链接检查。使用从实际 ORM 复制的 SQLite 内存表及实际 WebApp／Conversation 服务方法，验证账号／应用隔离、映射复用、唯一索引、旧会话归属、多个未绑定使用者及 OpenAPI 应用权限；仅为 SQLite 适配了 PostgreSQL 的类型转换默认值、时间与 UUID 默认值。

该隔离检查没有启动完整 HTTP／运行时依赖，也未覆盖真实 PostgreSQL 并发、API 凭证或微信回调，不代表实际数据库迁移及渠道功能已完成验收。
