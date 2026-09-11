# Agent Core 阅读入口

Core 接收平台已解析的模型、工具和 Skill，通过 DeepAgents 构图并运行。应用权限、草稿与发布版本、Run 数据库记录和 SDK 响应都由外层服务处理。

| 要修改的行为 | 阅读入口 |
| --- | --- |
| 一次运行如何创建资源和执行器 | `assembly/runtime_assembler.py` |
| 主图、子图及中间件组成 | `assembly/graph/deep_agent_factory.py` |
| 已解析子 Agent 到框架规格的转换 | `assembly/graph/subagent_spec_adapter.py` |
| 执行、取消、恢复与结果提取 | `execution/agent_executor.py` |
| 输出审核与原生事件交付 | `execution/event_delivery.py` |
| 一次运行资源的关闭 | `execution/runtime_resources.py` |
| 沙箱提供者选择及会话复用 | `backends/sandbox/sandbox_backend_factory.py` |
| 工作空间同步和版本冲突 | `backends/sandbox/sandbox_workspace_sync.py` |
| Skill 路由和长期记忆路由 | `assembly/context/skill_context.py`、`memory_context.py` |
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

`on_event` 和 `cancel_requested` 可选：工作台传入事件接收方和共享取消信号；微信兼容入口只取最终结果。没有事件订阅者时仍执行输出审核，但不缓存待交付事件。`request_cancel()` 同时设置本次取消信号并请求图排空，调用方无需额外设置 Event；未启动或已结束时返回 False。数据库取消轮询、Session 和执行租约由 Service 管理。

原生 `stream_events`、`Command` 和 `RunControl` 留在执行器内部。所有会话配置由 `AgentRunContext.conversation` 生成，调用方不再传入任意图配置。每个执行器只执行一次，返回结果前关闭独占资源；需要恢复时重新装配执行器并沿用会话身份与 Checkpointer。

`AgentExecutionResult` 的优先级为：执行或清理错误 → `failed`；取消 → `cancelled`；存在中断 → `waiting_input`；其余 → `completed`。只有等待填写时返回 interrupts；开启输出审核后，失败或取消结果移除 messages，避免回答进入业务历史。业务 Run 状态、消息落库和观测历史合并由 Service 处理。

## 资源所有权

装配器使用 `ExitStack` 登记已创建资源的关闭回调：Workspace 创建后立即登记；Skill 构造完成后登记它的路由。Sandbox 接管 Workspace 时替换其关闭登记；CompositeBackend 成功创建后整体接管独占 Backend。

资源获取和图编译分别由装配器内部方法负责，Store 与 Checkpointer 工厂可注入。构造失败时关闭已经取得的资源并保留原始异常；成功后由 `AgentRuntimeResources` 持有组合 Backend，执行器通过它释放资源。清理只执行一次：成功可重复关闭；失败持续报告原始异常，执行器保留 `CLOSE_FAILED`，不自动重跑已释放会话的沙箱同步。进程共享的 Checkpointer 与 Store 不随单次运行关闭。

沙箱工厂直接返回 Backend，已移除只包装一个 Backend 的 `SandboxProjection` 和无状态转发类。不同沙箱提供者的适配保留在各自文件中。

Skill 与 Memory 使用 `build_skill_context`、`build_memory_context` 直接构建结果，不再为一次调用创建无状态装配对象。Memory 关闭时不会调用 Store 工厂；Skill 路由构建失败仍由构建函数清理已创建的部分资源。

## 与服务层的分工

Core 的 `PendingUserInput` 验证表单字段与答案。平台的 `AgentRunInteraction` 负责表单提交幂等、取消批次及重新填写规则。前者不认识数据库 Run，后者不直接调用 LangGraph。

可保存的子 Agent 配置位于 `internal/entity/subagent_config.py`，平台服务负责发布应用授权、模型继承、连接解析及工具选择。Core 只接收 `SyncSubAgentDefinition` 或 `AsyncSubAgentDefinition`，不再同时接收配置和独立绑定字典。跟随模式在每次装配时直接绑定本轮父模型实例。

测评中间件同样属于 Core：工具白名单和 Rubric 状态适配不依赖数据库或任务队列。测评 Service 负责应用授权、冻结配置和持久化，通过 `on_progress` 回调接收每轮记录；这些中间件只挂载到独立测评图。

输出审核由图中间件保护状态写入，`AgentEventDelivery` 保护事件公开，两处检查职责不同。开启审核时仍需缓存整轮原生事件；失败和取消立即丢弃缓存，不对任意工具对象增加序列化要求。

长期记忆统一使用 `/memories/` 下的分类 Markdown 文件，StoreBackend 按账号与应用隔离存储。`ReloadingMemoryMiddleware` 每次模型请求刷新只读目录 `.index.md`，文件增改复用原生工具，删除使用 `delete_memory_file`。文件仓储负责存取、容量限制和内容摘要并发校验；列表查询只读，不做旧目录导入，也不维护结构化条目、修订历史或导入标记。

目录排版由 `render_memory_index` 消费文件快照，不访问 Store；目录与偏好正文的 UTF-8 字节预算在该函数所在模块维护。HTTP 管理接口与模型工具共用领域层的可写路径规则，新建、覆盖、重命名源/目标和删除都禁止修改系统索引。模型编辑在同一事务内读取、替换、校验和写回，文件格式转换与时间戳更新复用框架实现。
