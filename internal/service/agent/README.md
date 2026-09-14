# Agent 服务阅读入口

从 `agent_run_service.py` 的公共方法开始阅读：类顶部是命令（启动、重启、提交答案、重填、取消），中部是 Run 持久化与查询，底部是后台执行编排。

业务角色、入口差异和生命周期规则见 [Agent 业务文档](../../../docs/Agent-业务文档.md)；表关系、字段、约束与事务映射见 [业务建模自梳理](../../../docs/业务建模自梳理.md)。

| 文件 | 职责 |
| --- | --- |
| `agent_run_service.py` | Run 聚合唯一入口：命令、持久化、孤儿收敛与有界后台执行 |
| `agent_run_query.py` | Run 状态、模型选项、历史事件和 SSE 事件流；自己执行只读投影查询，只读已提交数据 |
| `agent_run_events.py` | 进程内 Run 跟踪（登记、取消信号、订阅通知）与事件批量落库、首字交付 |
| `agent_model_service.py` | 默认模型来源、手动覆盖、连接授权、思考强度和模型目录 |
| `agent_definition_service.py` | 读取草稿／发布／内置定义及恢复指纹，不解析模型连接 |
| `agent_factory.py` | 一次读取应用定义，直接生成 AgentConfig 与运行上下文，交给 Core 装配执行器 |
| `creator_task_service.py` | 创建者任务页的应用与会话查询、更新、删除 |
| `assistant_agent_service.py` | 内置助手的会话入口和专属工具 |
| `agent_attachment_service.py` | 附件归属校验与 COS 内容读取 |
| `agent_conversation_cleanup_service.py` | 会话、消息及应用关联运行资源的清理 |

## 一次对话

1. `start_run` 锁定会话，检查重复请求和冲突，创建消息及 Run。
2. 事务提交后调用 `_submit_execution` 投递后台执行；后台调用装配工厂创建当前 Run 的执行器。
3. Core 规范化事件经 `agent_protocol` 绑定业务身份和编号，再由事件缓冲写入数据库。
4. `_commit_result` 将结果、最终快照和终态事件原子落库；提交成功后更新批次末尾游标，只通知订阅者一次。
5. 查询服务的 `stream_events` 读取已提交事件；进程内通知只是唤醒机制，数据库是重连的数据源。

`input_message_id` 用于对齐客户端即时显示的消息、后端历史和执行时间线，也用于后端识别同 ID 的重复启动。当前前端没有配置启动命令自动重试；SSE 重连只续传事件。发送失败后重新点击发送会生成新的消息 ID，不能将其视为必然复用原 Run。

时序、资源所有权与验证步骤见 [Agent 业务文档](../../../docs/Agent-业务文档.md) 的 Run 生命周期与执行链路两节。

会话状态接口只返回消息、Run 摘要和公开状态，不加载执行事件。执行过程由历史分页和实时 SSE 提供；前端共用一个事件集合，运行摘要不再转换为合成事件。

事件投影器直接返回 `WorkbenchEvent`，事件缓冲只批量保存与通知。框架消息解析、摘要过滤及消息结束分片由 Core 完成；`to_public_event` 仅用于读取历史 JSON。

终态展示由 `agent_protocol.state_projector` 的 `project_execution_state` 和 `project_execution_lifecycle` 生成；`AgentRunService` 保留数据库读取、业务消息状态、错误脱敏和原子提交顺序。

后台执行只向 Core 传入 `message` 或 `resume`、事件接收方和取消信号，不构造 LangGraph Command。取消监控只等待进程内取消信号，收到后调用 `AgentExecutor.request_cancel()` 请求图排空；阻塞中的工具只有主动排空才会立刻停止。微信兼容入口也使用 `execute(message=...)`，直接保存结果中的回答与用量。

## 待填写与取消

- `restart_run`（内部 SDK 更新）：先校验新输入，再取消并有界等待旧执行释放，最后沿用正常启动事务。
- `submit_answers` 复核答案、身份和配置指纹，提交后才恢复图执行。
- 前端补充表单在提交失败后保留答案，允许按同批中断 ID 再次提交；Submission 记录本批已接受答案和游标，避免重复恢复。它不保存 Agent 最终回复。
- `reopen_form` 只恢复已取消表单，不启动图，也不占用执行权。
- `_request_cancel` 对活动运行请求停止；没有执行线程的待填运行直接原子取消。
- 同一会话的操作先锁会话，再锁 Run；重构时保留各入口原有的 Run 查询范围。
- `internal/entity/agent_run_interaction.py` 集中维护表单校验、批次身份和取消恢复规则；已提交答案直接查询 `AgentRunSubmission`，不扫描快照数组。
- `_queue_resumed_run` 在调用方事务内保存 Submission、交互投影及 Run 的新执行标识；`_reopen_pending_run` 只恢复交互状态，命令方法不直接修改这些字段。

## 修改时的边界

- 应用配置来源由定义服务管理，恢复指纹由调用方 `load(...).fingerprint()` 直接比较，不经单行转发；模型服务统一处理默认来源、覆盖校验、历史选择恢复和失效回退；恢复过程中只读取一次默认配置，失效覆盖不把旧思考强度带入回退模型。查询服务自己读取历史覆盖字段，只把解析委托给模型服务。
- 只读投影（状态 Run 列表、历史事件分页、续传批次、模型覆盖字段）由查询服务自己查询；写服务只保留喂给写决策的读取，以及必须与写路径共用锁语义的 `get_conversation_run`、`get_run_status`。
- 命令方法与持久化同属 `AgentRunService`：公开方法编排事务和业务规则，下划线方法只做数据库读写，两者不再跨服务转发；锁定查询后不关闭 Session，Session 生命周期由入口负责。
- 公开面只保留真正被外部调用的成员：五个命令（`start_run` / `restart_run` / `submit_answers` / `reopen_form` / `cancel_run`）加四个只读入口（`get_run` / `get_run_status` / `get_run_metadata` / `get_conversation_run`）。执行链路步骤、锁、事件与结算一律下划线，同模块内引用，不改动它们的外部契约。
- 只有单一调用者的查询不再抽成方法：占用检查、幂等查重、事件序号读取、最新 Run 读取都直接写在调用点，靠 `# 1.` 步骤注释和局部变量名表达意图。因此同一段查询不因"加锁/不加锁"分叉成两个方法，也不留布尔开关参数。
- 装配工厂不依赖 Run 服务：近期答案由 `AgentFactory._load_confirmed_inputs` 自己读取，避免装配层反向依赖持久化层，也避免模块循环导入。
- `get_run`、`get_run_status`、`get_run_metadata` 是只读入口；`get_conversation_run` 单次按应用/会话/入口过滤校验归属。孤儿收敛只收业务筛选条件，存活集合由 Run 服务从进程内跟踪器自取；需要收敛时命令方法完成归属校验后显式调用 `_settle_orphan_runs`，并负责 Session 生命周期。
- `_mark_running` 只处理执行启动，终态统一交给 `_finalize_run`；本地 `AgentRunExecution` 只保留执行身份、游标及线程信号，状态和错误以数据库为准。
- `AgentRun.is_terminal` 判断任务终态；Run 服务只锁定 Run，校验 executor_id 与活动状态。结算在同一事务中清空执行标识并通过 `Message.apply_usage()` 累计用量，重复结算和旧执行均失去写入资格。模型方法不查询或提交数据库。
- 提交答案锁定当前应用和入口的最新 Run；重新填写检查整个会话的最新 Run，不能跳过其他入口后来创建的任务。启动占用检查与数据库唯一索引均覆盖整个会话，包括 waiting_input。
- Run 的调用参数、交互状态和展示投影分别保存；读取公开状态使用 `public_snapshot`，执行快照更新使用 `apply_snapshot()`。取消、恢复与结算共用事件追加方法，事件与游标不能分开提交。
- 消息、表单、生命周期的协议结构集中在相邻的 `agent_protocol` 目录。
- 事件只能在提交后通知；执行标识失效的执行实例不能覆盖后续结果。序号查询使用调用方事务，不自行关闭 Session。
- SSE 信号只维护通知版本；查询服务直接按会话、入口、Run 和游标读取已提交事件，不跨订阅缓存正文。孤儿 Run 由进程内注册表判定，写入口（启动/取消）顺带收敛，读入口只读不写。
- 助手和普通应用共用类型化模型配置、覆盖规则及加载路径；助手服务只构造专属工具。
- 已发布子 Agent 的模型由其发布配置决定，自定义跟随子 Agent 绑定本次父模型。
- SDK 的命令 ID、分派及成功／错误外壳由 `agent_protocol/command_adapter.py` 处理，业务命令返回 `RunAccepted` 或 `RunRejected`。
- `EventBatch` 是 `AgentRunQuery` 内部扫描结果，保留扫描游标，不作为 HTTP 响应 Schema。
- 公开事件通道词表只在 `agent_workbench_schema.PUBLIC_EVENT_METHODS` 定义一次：DTO 校验的 `Literal`、历史回放过滤和持久化查询都从它派生，新增通道只改这一处。

## 数据对象的位置

- `internal/entity/agent_invocation.py`：可信调用身份和可恢复输入；保持历史 JSON 字段格式，供业务服务和运行存储使用。
- `internal/schema/agent/agent_model_schema.py`：工作台模型选项的公开响应。
- 清理入口直接接收已授权的 UUID 关键字参数；消息清理只接收会话和消息 ID，不构造中间请求对象。
- `ApplicationAgentDefinition`：一次读取的应用名称、所有者和配置，替代需要记住字段位置的四元组。

## Core 依赖入口

运行装配、执行、事件缓冲及 Run 持久化统一从 `internal.core.agent_v2` 获取执行器、观测入口、错误分类和恢复上下文。执行路径不从具体中间件实现导入异常，也不自行拼接 psycopg 错误集合；异常分类及池诊断入口由 Core 提供。用量值对象及归一化规则位于 `internal/entity/token_usage.py`，ORM 模型不依赖 Agent 运行时。

装配工厂仍负责模型/工具/Skill 授权、可信上下文和附件准备，Workspace 存储适配器以已创建对象交给 Core。附件服务返回已授权文件的路径和字节，Core 负责写入本轮副本；框架 Store、Checkpointer 和 Backend 装配均在 AgentV2 内完成。Core 的事件与结果状态已经转换为 JSON 兼容数据，Service 不解释框架消息类型。会话删除属于业务资源清理，持久化和沙箱删除适配器保留独立入口。

后台调度的线程池、进程内取消信号和公开错误映射仍在平台层；`AgentRunService` 负责数据库锁、事件/消息/快照的原子提交，观测合并函数只处理数据。事件缓冲通过 `_record_events` 回调提交，不反向依赖服务类。

内部异步子 Agent 的 SDK 服务与鉴权位于 `../internal_agent_protocol`，前端事件投影保留在 `../agent_protocol`。
