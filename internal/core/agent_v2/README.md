# Agent Core 阅读入口

Core 接收平台已解析的模型、工具和 Skill，通过 DeepAgents 构图并运行。应用权限、草稿与发布版本、Run 数据库记录和 SDK 响应都由外层服务处理。

| 要修改的行为 | 阅读入口 |
| --- | --- |
| 一次运行如何创建资源和执行器 | `assembly/runtime_assembler.py` |
| 主图、子图及中间件组成 | `assembly/graph/deep_agent_factory.py` |
| 子 Agent 工具与模型绑定 | `assembly/graph/subagent_spec_adapter.py` |
| 执行、取消、恢复与结果提取 | `execution/agent_executor.py` |
| 一次运行资源的关闭 | `entity/runtime_resources.py` |
| 沙箱提供者选择及会话复用 | `backends/sandbox/sandbox_backend_factory.py` |
| 工作空间同步和版本冲突 | `backends/sandbox/sandbox_workspace_sync.py` |
| Skill 路由和长期记忆路由 | `assembly/context/skill_context.py`、`memory_context.py` |
| 框架 Checkpoint 与 Store | `persistence/` |
| 模型调用、工具调用与用量记录 | `observability/run_observer.py` |
| 测评工具访问限制 | `middleware/evaluation_tool_middleware.py` |
| Rubric 评分回调、修订与每轮验收 | `middleware/evidence_rubric_middleware.py` |
| 测评候选提取与确定性证据核验 | `execution/evaluation_evidence.py` |

## 资源所有权

装配器使用 `ExitStack` 登记已创建资源的关闭回调：Workspace 创建后立即登记；Skill 构造完成后登记它的路由。Sandbox 接管 Workspace 时替换其关闭登记；CompositeBackend 成功创建后整体接管独占 Backend。

构造失败时，装配器关闭已经取得的资源并保留原始异常。构造成功后，关闭回调交给 `AgentRuntimeResources`，执行器通过它释放资源。回调消费一次，重复关闭不重复释放。进程共享的 Checkpointer 与 Store 不随单次运行关闭。

沙箱工厂直接返回 Backend，已移除只包装一个 Backend 的 `SandboxProjection` 和无状态转发类。不同沙箱提供者的适配保留在各自文件中。

Skill 与 Memory 使用 `build_skill_context`、`build_memory_context` 直接构建结果，不再为一次调用创建无状态装配对象。Memory 关闭时不会调用 Store 工厂；Skill 路由构建失败仍由构建函数清理已创建的部分资源。

## 与服务层的分工

Core 的 `PendingUserInput` 验证表单字段与答案。平台的 `AgentRunInteraction` 负责表单提交幂等、取消批次及重新填写规则。前者不认识数据库 Run，后者不直接调用 LangGraph。

修改运行中的模型继承时，先检查服务层本次解析得到的父模型及子模型绑定；Core 使用已经解析好的实例，不读取应用配置。

测评中间件同样属于 Core：工具白名单和 Rubric 状态适配不依赖数据库或任务队列。测评 Service 负责应用授权、冻结配置和持久化，通过 `on_progress` 回调接收每轮记录；这些中间件只挂载到独立测评图。
