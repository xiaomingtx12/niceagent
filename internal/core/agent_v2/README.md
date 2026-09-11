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
| 用户提问工具装配与暂停识别 | `middleware/user_input_middleware.py`、`observability/user_input_interrupt_tracker.py` |
| 框架 Checkpoint 与 Store | `persistence/` |
| 模型调用、工具调用与用量记录 | `observability/run_observer.py` |
| 测评工具访问限制 | `middleware/evaluation_tool_middleware.py` |
| Rubric 评分回调、修订与每轮验收 | `middleware/evidence_rubric_middleware.py` |
| 测评候选提取与确定性证据核验 | `execution/evaluation_evidence.py` |

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
