# Agent 服务阅读入口

从 `agent_run_service.py` 的公共方法开始阅读：类顶部是命令（启动、重启、提交答案、取消），中部是 Run 持久化与查询，底部是后台执行编排。

交互规则见 [Agent 最小交互业务约定](../../../docs/Agent-最小交互业务约定.md)；最终表结构及落库前的数据保留要求见 [会话与执行表定案](../../../docs/Agent-会话与执行表定案.md)。其他业务背景见 [Agent 业务文档](../../../docs/Agent-业务文档.md)。

| 文件 | 职责 |
| --- | --- |
| `agent_run_service.py` | Run 聚合唯一入口：命令、持久化、孤儿收敛与有界后台执行 |
| `agent_run_query.py` | Run 状态、历史快照及 SSE；已保存前缀与实时尾部按序衔接 |
| `agent_run_events.py` | 进程内 Run 跟踪、实时发布、独立写入线程及结束前排空 |
| `live_event_stream.py` | 活动执行的有界事件尾部；实时交付及刷新后补接未落库内容 |
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
3. Core 规范化事件经 `agent_protocol` 绑定业务身份和编号，立即发布到内存尾部，由独立线程合批写库。
4. `_commit_result` 将结果、最终快照和终态事件原子落库；提交成功后更新批次末尾游标，只通知订阅者一次。
5. SSE 从已提交快照接到内存尾部，慢订阅者超出保留范围时从数据库补齐；正常实时交付不查询数据库。

后台代码从 `_execute_run` 顺着读：它负责执行、提交事件尾批和保存最终结果。`_execute_agent` 只呈现启动、发布准备状态、装配和执行四步；启动事务与取消复查在 `_mark_run_running`，取消监控的启动和退出在 `_execute_with_cancel_monitor`。Core 的 `execute` 负责资源生命周期，`_run_graph` 负责消费框架事件。各步骤直接调用，不再通过额外生成器转交执行控制。

`input_message_id` 用于对齐客户端即时显示的消息、后端历史和执行时间线，也用于后端识别同 ID 的重复启动。当前前端没有配置启动命令自动重试；SSE 重连只续传事件。发送失败后重新点击发送会生成新的消息 ID，不能将其视为必然复用原 Run。

时序、资源所有权与验证步骤见 [Agent 业务文档](../../../docs/Agent-业务文档.md) 的 Run 生命周期与执行链路两节。

会话状态接口只返回消息、Run 摘要和公开状态，不加载执行事件。执行过程由历史分页和实时 SSE 提供；前端共用一个事件集合，运行摘要不再转换为合成事件。

事件缓冲的 `receive` 直接筛选 Core 事件、适配载荷并绑定业务消息 ID，调用共用编号器生成 `AgentProtocolEvent` 后合批提交。Core 解析框架消息并过滤摘要，前端根据真实终态收尾未完成记录；`parse_stored_event` 仅用于读取历史 JSON。

每个活动片段独占一个写线程和 Flask 应用上下文，不跨线程共用数据库 Session。每批最多 128 个事件，未凑满时等待最多 250ms 后提交，空闲小批次也会保存；待写队列最多 512 个事件，实时尾部保留最近 2048 个事件。先登记待写队列再实时发布，只有持续写入拥塞才施加背压，因此正常情况下淘汰的历史都已落库。结束前 `finish()` 排空并回收线程，异常尾批在调用线程重试一次；仍失败则只能按失败结算，不能宣告成功。片段退出后释放内存尾部，后续历史统一读库。

`_commit_result` 直接构造执行结果状态与待填写表单，`_prepare_result_status` 直接生成业务消息状态和公开生命周期。待填写任务与聊天历史直接在 `AgentRunQuery.get_state` 内构造；`state_projector` 只保留实时事件和状态查询共用的转换。

后台执行只向 Core 传入 `message` 或 `resume`、事件接收方和取消信号，不构造 LangGraph Command。取消监控只等待进程内取消信号，收到后调用 `AgentExecutor.request_cancel()` 请求图排空；阻塞中的工具只有主动排空才会立刻停止。微信兼容入口也使用 `execute(message=...)`，直接保存结果中的回答与用量。

## 待填写与取消

- `restart_run`（内部 SDK 更新）：先校验新输入，再取消并有界等待旧执行释放，最后沿用正常启动事务。
- `submit_answers` 复核答案、身份和配置指纹，提交后才恢复图执行。
- 前端补充表单在提交失败后保留答案，允许按同批中断 ID 再次提交；Submission 记录本批已接受答案和游标，避免重复恢复。它不保存 Agent 最终回复。
- 已停止 Run 不可重开；旧 `input.reopen` 命令返回明确拒绝，下一条普通消息创建新 Run。
- `_request_cancel` 对活动运行请求停止；没有执行线程的待填运行直接原子取消并清空待填表单；新输入由 LangGraph 丢弃旧调度，DeepAgents 的工具回执修复保留可用历史。
- 同一会话的操作先锁会话，再锁 Run；重构时保留各入口原有的 Run 查询范围。
- `internal/entity/agent_run_interaction.py` 集中维护表单校验、批次身份和已确认问答生成；已提交答案直接查询 `AgentExecutionSubmission`，不扫描快照数组。
- `submit_answers` 在调用方事务内保存 Submission、`input_history` 及新执行标识，并清空 `pending_inputs`；接受回执和恢复执行由同一事务决定。
- 只有单一调用点的小步骤（≤25 行）直接写在调用方：提交幂等查重、恢复后的答案保存、执行启动、待填取消、Run 建行都在命令方法内联。步骤注释连续编号表达顺序，不再为它们保留只转发不判断的方法。

## 修改时的边界

- 应用配置来源由定义服务管理，恢复指纹由调用方 `load(...).fingerprint()` 直接比较，不经单行转发；模型服务统一处理默认来源、覆盖校验、历史选择恢复和失效回退；恢复过程中只读取一次默认配置，失效覆盖不把旧思考强度带入回退模型。查询服务自己读取历史覆盖字段，只把解析委托给模型服务。
- 只读投影（状态 Run 列表、历史事件分页、续传批次、模型覆盖字段）由查询服务自己查询；写服务只保留喂给写决策的读取与 `get_run_status`。
- 命令方法与持久化同属 `AgentRunService`：公开方法编排事务和业务规则，下划线方法只做数据库读写，两者不再跨服务转发；锁定查询后不关闭 Session，Session 生命周期由入口负责。
- 公开面只保留真正被外部调用的成员：四个命令（`start_run` / `restart_run` / `submit_answers` / `cancel_run`）加三个只读入口（`get_run` / `get_run_status` / `get_run_metadata`）。执行链路步骤、锁、事件与结算一律下划线，同模块内引用，不改动它们的外部契约。
- 只有单一调用者的查询不再抽成方法：占用检查、幂等查重、事件序号读取、最新 Run 读取都直接写在调用点，靠 `# 1.` 步骤注释和局部变量名表达意图。因此同一段查询不因"加锁/不加锁"分叉成两个方法，也不留布尔开关参数。
- 装配工厂不依赖 Run 服务：近期答案由 `AgentFactory._load_confirmed_inputs` 自己读取，避免装配层反向依赖持久化层，也避免模块循环导入。
- `get_run`、`get_run_status`、`get_run_metadata` 是只读入口。Run 归属（应用 + 会话 + 入口 + Run 标识）由各调用点直接过滤：取消与 SSE 订阅只读 `id` 或整行，内部 SDK 读取整行；新增校验条件时必须三处同时改。孤儿收敛只收业务筛选条件，存活集合由 Run 服务从进程内跟踪器自取；需要收敛时命令方法完成归属校验后显式调用 `_settle_orphan_runs`，并负责 Session 生命周期。
- 执行启动只写运行态，终态统一交给 `_finalize_run`；本地 `AgentExecutionSegment` 只保留执行身份、游标及线程信号，状态和错误以数据库为准。
- `AgentExecution.is_terminal` 判断任务终态；Run 服务只锁定 Run，校验 executor_id 与活动状态。结算在同一事务中清空执行标识并通过 `ConversationTurn.apply_usage()` 累计用量，重复结算和旧执行均失去写入资格。模型方法不查询或提交数据库。
- 提交先在授权会话及入口查已接受批次回执，新答案才锁定最新 Run 校验。启动占用检查与数据库唯一索引均覆盖整个会话，包括 waiting_input。
- Execution 的 `execution_options`、`pending_inputs`、`input_history` 和 `display_state` 分别保存；`public_snapshot` 组合公开状态，`apply_snapshot()` 只更新展示白名单，不改变表单。取消、恢复与结算共用事件追加方法，事件与游标不能分开提交。
- 消息、表单、生命周期的协议结构集中在相邻的 `agent_protocol` 目录。
- 事件只能在提交后通知；执行标识失效的执行实例不能覆盖后续结果。序号查询使用调用方事务，不自行关闭 Session。
- SSE 信号只维护通知版本；查询服务先校验会话、入口和 Run 归属，再按游标读取已提交历史或活动片段的共享内存尾部。孤儿 Run 由进程内注册表判定，写入口（启动/取消）顺带收敛，读入口只读不写。
- 助手和普通应用共用类型化模型配置、覆盖规则及加载路径；助手服务只构造专属工具。
- 已发布子 Agent 的模型由其发布配置决定，自定义跟随子 Agent 绑定本次父模型。
- SDK 的命令 ID、分派及成功／错误外壳由命令入口 `respond_command` 处理：按已解析参数直接分派 `start_run` / `submit_answers`，业务命令返回 `RunAccepted` 或 `RunRejected`。
- SSE 续传在 `AgentRunQuery._stream_events` 内切换数据库前缀和实时尾部：游标按扫描序号前进，过滤只影响发送，隐藏事件不阻断续传。
- 公开事件通道词表只在 `agent_protocol_schema.PUBLIC_EVENT_METHODS` 定义一次：DTO 校验的 `Literal`、历史回放过滤和持久化查询都从它派生，新增通道只改这一处。

## 数据对象的位置

- `internal/entity/agent_invocation.py`：可信调用身份和可恢复输入；由授权身份、ConversationTurn 原始输入和 AgentExecutionOptions 组装；只将运行选项持久化。
- `internal/schema/agent/agent_model_schema.py`：工作台模型选项的公开响应。
- 清理入口直接接收已授权的 UUID 关键字参数；消息清理只接收会话和消息 ID，不构造中间请求对象。
- `ApplicationAgentDefinition`：一次读取的应用名称、所有者和配置，替代需要记住字段位置的四元组。

## Core 依赖入口

运行装配、执行、事件缓冲及 Run 持久化统一从 `internal.core.agent_v2` 获取执行器、观测入口、错误分类和恢复上下文。执行路径不从具体中间件实现导入异常，也不自行拼接 psycopg 错误集合；异常分类及池诊断入口由 Core 提供。用量值对象及归一化规则位于 `internal/entity/token_usage.py`，ORM 模型不依赖 Agent 运行时。

装配工厂仍负责模型/工具/Skill 授权、可信上下文和附件准备，Workspace 存储适配器以已创建对象交给 Core。附件服务返回已授权文件的路径和字节，Core 负责写入本轮副本；框架 Store、Checkpointer 和 Backend 装配均在 AgentV2 内完成。Core 的事件与结果状态已经转换为 JSON 兼容数据，Service 不解释框架消息类型。会话删除属于业务资源清理，持久化和沙箱删除适配器保留独立入口。

后台调度的线程池、进程内取消信号和公开错误映射仍在平台层；`AgentRunService` 负责数据库锁、事件/消息/快照的原子提交，观测合并函数只处理数据。事件缓冲通过 `_record_events` 回调提交，不反向依赖服务类。

内部异步子 Agent 的 SDK 服务与鉴权位于 `../internal_agent_protocol`，前端事件投影保留在 `../agent_protocol`。
