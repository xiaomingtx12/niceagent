# Agent Core 阅读入口

Core 接收平台已解析的模型、工具和 Skill，通过 DeepAgents 构图并运行。应用权限、草稿与发布版本、Run 数据库记录和 SDK 响应都由外层服务处理。

| 要修改的行为 | 阅读入口 |
| --- | --- |
| 一次运行如何创建资源和执行器 | `assembly/runtime_assembler.py` |
| CompositeBackend 的默认存储和路径路由 | `assembly/runtime_assembler.py` 中直接构造 |
| 主图、子图及 DeepAgents 参数 | `assembly/graph/deep_agent_factory.py` |
| 中间件启用条件、顺序、重试与调用限制 | `assembly/graph/platform_middleware.py` |
| 已解析子 Agent 到框架规格的转换 | `assembly/graph/subagent_spec_adapter.py` |
| 执行、取消、恢复与结果提取 | `execution/agent_executor.py` |
| 输出审核与交付生命周期 | `execution/event_delivery.py` |
| 框架事件规范化与文本分片 | `execution/event_normalizer.py` |
| 一次运行资源的关闭 | `execution/runtime_resources.py` |
| 沙箱提供者选择及会话复用 | `backends/sandbox/sandbox_backend_factory.py` |
| 本轮文件副本与 Sandbox 初始化 | `backends/workspace/workspace_projection.py`、`backends/sandbox/sandbox_initialization.py` |
| 文件按需加载、显式交付与版本冲突 | `execution/workspace_delivery.py`、`persistence/workspace_store.py` |
| Skill 路由和长期记忆路由 | `assembly/context/skill_context.py`、`assembly/runtime_assembler.py` |
| 文件记忆存取与并发校验 | `persistence/agent_memory_repository.py` |
| 文件记忆工具适配与配额事务 | `backends/memory/agent_memory_backend.py` |
| 记忆目录排版与提示词字节预算 | `backends/memory/memory_index.py` |
| 用户提问工具装配与暂停识别 | `middleware/user_input_middleware.py`、`execution/user_input_interrupt_tracker.py` |
| 摘要生成进度通知 | `execution/summary_progress.py` |
| 框架 Checkpoint 与 Store | `persistence/` |
| 模型调用、工具调用与用量记录 | `observability/run_observer.py` |
| 测评工具访问限制 | `middleware/evaluation_tool_middleware.py` |
| Rubric 评分回调、修订与每轮验收 | `middleware/evidence_rubric_middleware.py` |
| 测评候选提取与确定性证据核验 | `execution/evaluation_evidence.py` |

## 执行接口

平台通过 `AgentExecutor.execute(message=...)` 发起一次新输入，通过 `execute(resume=...)` 提交按中断 ID 组织的恢复答案。二者只能提供一个；空字典仍按恢复输入处理，不会误发新的用户消息。模型消息继续携带装配层设置的业务标识和附件信息。

`on_event` 和 `cancel_requested` 可选：工作台传入事件接收方和共享取消信号；微信兼容入口只取最终结果。没有事件订阅者时仍执行输出审核，但不缓存待交付事件。`request_cancel()` 同时设置本次取消信号并请求图排空，调用方无需额外设置 Event；未启动或已结束时返回 False。取消信号、Session 生命周期和执行标识由 Service 管理。

原生 `stream_events`、`Command` 和 `RunControl` 留在执行器内部。所有会话配置由 `AgentRunContext.conversation` 生成，调用方不再传入任意图配置。每个执行器只执行一次，返回结果前关闭独占资源；需要恢复时重新装配执行器并沿用会话身份与 Checkpointer。

`AgentExecutionResult` 的优先级为：执行或清理错误 → `failed`；取消 → `cancelled`；存在中断 → `waiting_input`；其余 → `completed`。只有等待填写时返回 interrupts；开启输出审核后，失败或取消结果移除 messages，避免回答进入业务历史。业务 Run 状态、消息落库和观测历史合并由 Service 处理。

## 资源所有权

装配器只维护一份待清理 Backend 列表，资源创建与所有权交接都在 `_build_runtime()` 中：

| 创建阶段 | 当前清理责任 |
| --- | --- |
| Workspace、Skill、Memory | Workspace 与 Skill 投影持有临时目录，交付存储持有独立仓储会话；Memory 使用进程共享 Store。 |
| Sandbox 成功返回 | 只负责本次 Provider 实例或租约，本轮工作副本仍由装配器持有。 |
| CompositeBackend 成功返回 | 接管 Backend 路由，临时投影和交付存储继续单独登记，后续构造失败也逐项释放。 |
| AgentRuntimeResources 成功返回 | 接管组合 Backend、临时投影和交付存储；先释放 Provider，再释放目录与会话，不提交任何工作文件。 |

`close_runtime_backends` 统一处理根 Backend 与直接路由的去重、继续清理和错误汇总。Sandbox 工厂只处理 Provider 自身资源；工作副本、Skill 投影和交付存储由运行资源分别持有，失败也逐项清理。

资源获取由运行时装配器负责，图编译直接接收完整 AgentConfig 和运行资源；Store 与 Checkpointer 由 AgentV2 内部按配置直接获取，平台只传入已授权的文件交付存储对象。构造失败时关闭已经取得的资源并保留原始异常；成功后由 `AgentRuntimeResources` 持有组合 Backend、临时投影和文件交付存储，执行器通过它释放资源。清理只执行一次：成功可重复关闭；失败持续报告原始异常，执行器保留 `CLOSE_FAILED`，不自动重跑已释放资源的关闭流程。进程共享的 Checkpointer 与 Store 不随单次运行关闭。

沙箱工厂直接返回 Backend，已移除只包装一个 Backend 的 `SandboxProjection` 和无状态转发类。不同沙箱提供者的适配保留在各自文件中。

Core 的 `AgentSkill` 只携带授权包内容，宿主目录归平台 Skill 服务管理。Skill 投影通过 `build_skill_context` 管理临时文件；Memory 在运行时装配处按开关直接创建 Backend，不再包装中间投影对象。运行资源只持有一份 Skill 投影，构图和中间件直接读取其来源字段。Memory 关闭时不会调用 Store 工厂；Skill 路由构建失败仍由构建函数清理已创建的部分资源。

## 与服务层的分工

Core 的 `PendingUserInput` 验证表单字段与答案。平台的 `AgentRunInteraction` 负责表单提交幂等、取消批次及重新填写规则。前者不认识数据库 Run，后者不直接调用 LangGraph。

可保存的子 Agent 配置位于 `internal/entity/subagent_config.py`，平台服务负责发布应用授权、模型继承、连接解析及工具选择。Core 只接收 `SyncSubAgentDefinition` 或 `AsyncSubAgentDefinition`，不再同时接收配置和独立绑定字典。跟随模式在每次装配时直接绑定本轮父模型实例。

测评中间件同样属于 Core：工具白名单和 Rubric 状态适配不依赖数据库或任务队列。测评 Service 负责应用授权、冻结配置和持久化，通过 `on_progress` 回调接收每轮记录；这些中间件只挂载到独立测评图。

输出审核由图中间件保护状态写入，`AgentEventDelivery` 保护事件公开，两处检查职责不同。开启审核时仍需缓存整轮原生事件；失败和取消立即丢弃缓存；通过审核的事件在 Core 出口转换为 JSON 兼容数据。

长期记忆统一使用 `/memories/` 下的分类 Markdown 文件，StoreBackend 按账号与应用隔离存储。`ReloadingMemoryMiddleware` 每次模型请求刷新只读目录 `.index.md`，文件增改复用原生工具，删除使用 `delete_memory_file`。文件仓储负责存取、容量限制和内容摘要并发校验；列表查询只读，不做旧目录导入，也不维护结构化条目、修订历史或导入标记。

目录排版由 `render_memory_index` 消费文件快照，不访问 Store；目录与偏好正文的 UTF-8 字节预算在该函数所在模块维护。HTTP 管理接口与模型工具共用领域层的可写路径规则，新建、覆盖、重命名源/目标和删除都禁止修改系统索引。模型编辑在同一事务内读取、替换、校验和写回，文件格式转换与时间戳更新复用框架实现。

## 中间件顺序与职责

`platform_middleware.py` 是平台策略的装配入口。平台链顺序固定为：Skill 授权刷新 → 工具消息校验 → Runtime Context → Review → 模型重试 → 只读工具重试 → 模型调用限制 → 工具调用限制 → 可选 Memory 刷新 → Context Window → 可选 User Input。调整分组不代表可以重排执行顺序。

DeepAgents 在平台链之前提供 Todo、Skills、Filesystem、SubAgent、摘要和工具调用补齐，尾部保留框架缓存及按权限生成的人工审批。平台 `ReloadingSkillsMiddleware(system_prompt=None)` 只刷新本轮授权元数据，官方 Skills 负责提示词，不重复注入；`memory=None` 则避免默认 Memory 与平台刷新逻辑重复。工具调用补齐负责中断后的消息配对，平台校验负责拒绝无效工具调用，不能互相替代。

`ContextWindowMiddleware` 观测当前模型请求，不负责摘要压缩。User Input 只在本轮允许提问且存在 Checkpointer 时加入主图，其提示词由 `agent_system_prompt.py` 统一构建。自定义子 Agent 显式绑定已授权工具、Skill 和权限；测评专用中间件由测评入口装配。

## 观测接口与消费者

LangChain Callback 自动累计模型和工具调用数及用量，业务入口创建 Observer、结束统计并调用 `build_run_usage()`。执行结果只交付一份片段用量，Run 服务将其累计到 Message；微信入口直接保存本次执行用量。

| 数据 | 消费者与保留原因 |
| --- | --- |
| Token、缓存/推理明细、调用数、分模型用量 | `Message.apply_usage()` 更新消息字段，Service 控制事务；Protocol 消息 metrics、工作台用量展示和 `AnalysisService` 报表使用。 |
| trace_id、executor_id | trace_id 关联消息与 Run；`executor_id` 标识当前执行段，在同一事务中校验执行权并阻止重复结算。 |

Observer 只保留活动模型的 ID/名称、活动工具 ID 及累计统计；不保存 Span、父子调用关系、阶段、诊断事件和执行片段列表。结束后拒绝迟到回调，重复结束不再次计费。装配、沙箱和首字交付保留已有日志；页面执行过程仍由 AgentRunEvent 提供。

## 文件交付模式

`/workspace/` 是本轮临时工作副本。无 Sandbox 时使用官方 `FilesystemBackend`；远程 Sandbox 启动前使用本轮本地副本，启动时只迁移已经加载或生成的文件，随后文件工具与 Shell 统一使用远程副本。本机 Shell 直接共用本轮目录。

持久化正文以 COS 为准，SQL 记录账号、应用、虚拟路径、版本及配额元数据。`list_saved_files` 只查目录，`load_saved_file` 按需下载一个文件；同轮重复加载不覆盖已经编辑的副本。附件仅准备到本轮副本，不自动进入持久化文件库。

`deliver_file` 在用户要求保存或交付时将指定产物写入 COS，并在成功后返回保存路径。未加载的目标仅允许新增，覆盖原文件必须使用加载时的版本；冲突可以加载最新文件到另一副本重新处理，或另存新路径。文件保存失败不会删除本轮副本，模型可在当前运行内重试。

资源关闭不扫描、回写或同步删除。临时修改、脚本、缓存不会自动持久化；修改和删除平台文件需要独立的业务操作。取消、失败或等待用户输入后，未交付文件不保证保留，恢复时应重新加载已保存输入并重建中间产物。平台预览与下载仍从 COS 获取已保存内容。

## 事件出口与框架隔离

`AgentEvent` 是业务订阅者使用的稳定数据契约：规范化通道、JSON 兼容 data、分支路径、节点及本次执行的递增序号。执行结果的 state 同样在 Core 出口转换为 JSON 兼容数据，业务层不接收 AIMessageChunk 或框架 metadata 元组。

`AgentEventDelivery` 先执行输出审核，通过后交给 `AgentEventNormalizer`。后者负责框架顺序校验、摘要过滤、消息和推理分片、供应商结束标记与并行分支收尾。每个分支只保留当前文本块；已结束块不再累积存储。无 finish 分片、异常或取消退出时，只补齐已经交付的文本，不公开审核缓存中的内容。

平台 `event_projector.py` 仅将 Core 事件绑定到业务 Run 和消息 ID，并生成持久化事件序号。Core 序号与数据库游标独立，工作台主动生成的状态和表单也使用平台游标。图结束事件可在 Core 出口出现，但业务终态必须等平台消息与快照原子提交后才对外发布。
