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
| 固定业务、规划与记忆提示词 | `assembly/configuration/agent_system_prompt.py` |
| 新输入的规划选项与公开用户原文 | `execution/model_input.py` |
| 记忆删除工具 | `middleware/agent_memory_middleware.py` |
| 用户提问工具装配与暂停识别 | `middleware/user_input_middleware.py`、`execution/user_input_interrupt_tracker.py` |
| 模型请求与摘要生成进度通知 | `execution/model_progress.py` |
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

Core 的 `AgentSkill` 只携带授权包内容，宿主目录归平台 Skill 服务管理。Skill 投影通过 `build_skill_context` 管理临时文件；Memory 在运行时装配处按开关直接创建 Backend，不再包装中间投影对象。运行资源只持有一份 Skill 投影，构图和中间件直接读取其来源字段。会话归档始终获取共享 Store，Memory 开关仅决定是否挂载长期记忆路由；Skill 路由构建失败仍由构建函数清理已创建的部分资源。

## 与服务层的分工

Core 的 `PendingUserInput` 验证表单字段与答案。平台的 `AgentRunInteraction` 负责表单提交幂等、取消批次及重新填写规则。前者不认识数据库 Run，后者不直接调用 LangGraph。

可保存的子 Agent 配置位于 `internal/entity/subagent_config.py`，平台服务负责发布应用授权、模型继承、连接解析及工具选择。Core 只接收 `SyncSubAgentDefinition` 或 `AsyncSubAgentDefinition`，不再同时接收配置和独立绑定字典。跟随模式在每次装配时直接绑定本轮父模型实例。

测评中间件同样属于 Core：工具白名单和 Rubric 状态适配不依赖数据库或任务队列。测评 Service 负责应用授权、冻结配置和持久化，通过 `on_progress` 回调接收每轮记录；这些中间件只挂载到独立测评图。

输出审核由图中间件保护状态写入，`AgentEventDelivery` 保护事件公开，两处检查职责不同。开启审核时仍需缓存整轮原生事件；失败和取消立即丢弃缓存；通过审核的事件在 Core 出口转换为 JSON 兼容数据。

长期记忆统一使用 `/memories/` 下的分类 Markdown 文件，StoreBackend 按账号与应用隔离存储。`AgentMemoryMiddleware` 只提供删除工具，固定规则在构图时装配，模型调用准备不读取 Store；模型按需使用原生 ls、glob、grep、read_file。文件增改复用原生工具，删除使用 delete_memory_file；没有自动目录、偏好注入和目录版本缓存。

文件仓储与 Backend 共用路径、容量及事务规则，管理接口保留内容摘要并发校验。编辑、上传或页面保存遇到相同路径与正文时不更新文件和时间，不发变更通知。模型编辑仍在同一事务内读取、替换、校验和写回。实际变更提交后发布 memory_file_update，身份沿用协议事件标识。其他会话修改在下一次实际读取时可见，既有工具消息不会自动替换。

## 中间件顺序与职责

`platform_middleware.py` 是平台策略的装配入口。平台链顺序固定为：可选异步工具 → Skill 授权刷新 → 工具消息校验 → 模型重试 → 只读工具重试 → 模型调用限制 → 工具调用限制 → 可选记忆删除工具 → Context Window → 可选 User Input。调整分组不代表可以重排执行顺序。

DeepAgents 在平台链之前提供 Todo、Skills、Filesystem、SubAgent、摘要和工具调用补齐，尾部保留框架缓存及按权限生成的人工审批。平台 `ReloadingSkillsMiddleware(system_prompt=None)` 只刷新本轮授权元数据，官方 Skills 负责提示词，不重复注入；`memory=None` 则明确关闭自动记忆加载，保留按需文件工具。工具调用补齐负责中断后的消息配对，平台校验负责拒绝无效工具调用，不能互相替代。

`ContextWindowMiddleware` 观测平台装配后的请求，不负责摘要压缩，估算不等于供应商最终序列化 Token。User Input 只在本轮允许提问且存在 Checkpointer 时加入主图，其提示词由 `agent_system_prompt.py` 统一构建。自定义子 Agent 显式绑定已授权工具、Skill 和权限；测评专用中间件由测评入口装配。

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

`AgentEvent` 是业务订阅者使用的稳定数据契约：规范化通道、JSON 兼容 data、分支路径、节点、模型调用 ID 及本次执行的递增序号。执行结果的 state 同样在 Core 出口转换为 JSON 兼容数据，业务层不接收 AIMessageChunk 或框架 metadata 元组。

`AgentEventNormalizer` 只适配框架有序 v3 事件：过滤摘要，使用框架转换器展开完整消息，并保留原生消息边界与模型调用身份。Core 不缓存正文、不推断消息成功结束。消息错误、取消和图终态交由前端已有的事件投影收敛未完成记录，保留已显示文本；运行成功也不会把缺少原生结束事件的消息标成成功。

平台 `AgentRunEventBuffer.receive` 直接筛选 Core 事件并绑定业务消息 ID，`event_projector.py` 为它与业务主动生成的状态和表单共用持久化事件编号。Core 序号与数据库游标独立。图结束事件可在 Core 出口出现，但业务终态必须等平台消息与快照原子提交后才对外发布。

## 上下文的动静分离

v3 的 `custom` 通道由每个图中唯一的 `ContextWindowMiddleware` 注册 `CustomTransformer`。记忆、模型进度和摘要通知仅向共享 writer 发布数据，不在各自中间件重复声明 transformer；框架合并注册列表时不会去重，重复声明会导致流初始化失败。

`assembly/configuration/context_budget.py` 按配置容量与输出预留复制模型 profile，原生自动和主动压缩共用预算。缺少窗口时沿用平台默认 400000 token；现有默认输入上限 272000 和输出上限 128000 token 参与预算，界面标明默认窗口。圆环显示压缩器计数与自动压缩阈值的比例，模型窗口占比在详情单独展示。`middleware/conversation_compaction.py` 复用框架主动工具，补齐媒体、重复调用与摘要失败保护。

`backends/conversation_history.py` 为 `/conversation_history/`、`/large_tool_results/` 提供作用域隔离、原子追加与有限容量；模型只读。`persistence/conversation_summary.py` 集中读取框架已提交摘要，执行器仅首尾核对并通知最新有效提交。各会话入口的 `/summary` 按需读取正文；具体阈值、限额及观测边界见升级方案第 12.1 节。

固定业务、规划模式解释、记忆及 Shell 路径规则在构图前装配。异步工具说明复用原生中间件生成的文本并提前加入 system，关闭重复追加；Skills 授权元数据在刷新后稳定排序，保留原生目录格式。

平台 system 按“固定进度与文件规则 → 固定任务数据与规划规则 → 应用名称、说明和业务指令 → 可选能力规则”排列。固定前缀不包含应用变量、本轮选项或时间戳；修改应用配置不会改写前面的固定内容。拼接方式本身不决定缓存命中，实际还取决于供应商请求中的工具定义、框架追加内容及缓存门槛。此次顺序调整会改变原请求前缀，上线后需要重新建立相应缓存，不代表已有缓存能够直接沿用。

AgentExecutor 只为新 HumanMessage 加入一次本轮自动/显式规划选项，保留身份和媒体；恢复只传 Command(resume)。事件与状态公开前移除内部选项，业务原文不改变。ask_user 答案通过原生 ToolMessage 留在对应位置，已删除前置汇总中间件、运行上下文字段和近期表单查询；公开 input_history 与 Submission 继续保留。

显式规划只通过固定规划提示词和本轮输入选项表达，沿用框架原生 `write_todos` 与 `todos` 状态。平台不增加强制规划中间件、私有规划标记或指定工具参数；提示词引导不作为确定性的执行保证。

前端在流式执行时间线展示 `write_todos` 工具调用及其输入、结果和失败，不额外展示结构化计划卡片。当前计划清单沿用对话输入框上方的计划区域，历史变更通过工具调用记录查看。

## 装配耗时与复用边界

模型加载从同次租户查询的连接记录中取得启用模型与密钥，避免额外查询密钥。`AgentFactory._resolve_agent_config` 创建局部 `ModelConnectionCache`，显式传给草稿配置解析、模型选择、主模型和同步子 Agent 加载；键包括租户、提供方和连接 ID。未指定连接时取得的完整提供方结果，可供本轮后续按 ID 筛选使用；某个连接的局部结果不能代替全部连接查询，仍保留同名模型歧义检查。仅复用数据库连接配置记录，不共享新建的模型实例或不同参数；入口选择权限和模型启用规则仍按原流程检查。每次新装配、恢复都会新建空字典，其他业务不传该参数时沿用实时查询。

草稿运行通过 `_process_and_validate_tools(batch_lookup=True)` 批量校验工具。API 工具与提供方联查一次，MCP 工具与启用服务联查一次；输出仍保留原顺序、名称、ID 和展示字段，避免本次性能修改改变等待表单的定义指纹。重复记录使用平台 `ValidateErrorException`；失效工具仍过滤。没有知识库或工作流引用时跳过相应空查询。管理页面的草稿读取、保存和发布路径不启用这一批量分支。

普通工具实例装配中，MCP 服务和 API 工具各最多一次批量查询，API 请求头随工具连接查询取得；同一 MCP 服务目录只解析一次，最终工具仍按绑定顺序排列。草稿校验与工具实例装配目前各有自己的批量查询，没有把 ORM 工具记录跨服务长期缓存。

没有 Skill 绑定时跳过目录加载。已有 Catalog 的不可变文件映射直接传入 Core；本轮内文件列表完全一致的 Skill 授权范围复用同一临时目录，不同授权集合保持隔离。临时目录仍在本轮结束后清理，下一轮重新创建，没有引入跨轮文件缓存。

`agent_assembly_timing` 按应用和会话记录阶段耗时：

| stage | 范围 |
| --- | --- |
| `config / model / tools / subagents / skills` | 应用配置、模型加载、工具准备、子 Agent 定义、Skill 解析 |
| `input / attachment_download / workspace_store` | 消息构造、COS 附件下载、本轮文件仓储绑定 |
| `runtime` | Core 运行资源装配合计 |
| `runtime.workspace / runtime.skills` | 工作目录创建、Skill 临时文件写入 |
| `runtime.store / runtime.checkpointer` | 获取共享持久化资源，首次访问可能包含连接和 setup |
| `runtime.sandbox` | Sandbox Backend 构造；远程 Provider 的实际延迟初始化不计入此阶段 |
| `attachments / graph` | 已下载附件写入工作副本、主图及子图构建 |
| `total` | 上述整个装配过程，不含后续模型推理 |

`runtime.*` 已包含在 `runtime` 中，不应重复相加。模型加载另有 `model_load_timing`，区分配置解析与 SDK 客户端构造；MCP 目录缓存未命中时记录 `mcp_tool_catalog_load`。日志只记录标识、数量和耗时，不记录密钥、请求头或文件正文。

本轮只做静态检查，未运行测试或性能请求；优化收益需对比同一配置在重启后首次请求和后续请求的日志，不能从减少查询次数推算实际提速秒数。
