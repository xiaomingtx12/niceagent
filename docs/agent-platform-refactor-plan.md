# Agent 平台改造优化规划

## 目标

在保留现有运行行为的前提下，降低 LangGraph 到 DeepAgents 迁移后的阅读和维护成本。

状态：阶段 0–3 已完成本地改造与行为验证，实际环境验收待完成。阶段 4–8 保持为后续评估项，不要求逐项新增模块。本文基于 2026-09-11 工作区并随实施更新；后续阶段开始前需重新核对现状。

运行时序、资源与事务约束、人工验收清单见 [Agent 运行基线](agent-runtime-baseline.md)。

核心原则：

- DeepAgents 负责 Agent harness：规划、文件工具、子 Agent 和基础 Agent loop。
- Core 负责一次运行的资源、执行和生命周期。
- Service 负责平台业务、事务和权限。
- Agent Protocol 负责事件与 SDK 协议转换。
- 每个阶段单独提交，先收紧接口，再移动实现，最后删除旧代码。
- DeepAgents 仍运行在 LangGraph 上，保留必要的 Checkpointer、Store、Command 和图执行依赖。
- 优先修改已有模块；只有明确减少调用方职责时才新增类或文件，不预设 Runner、Finalizer、Provider 等包装层。

## 当前问题

一次运行需要同时理解配置解析、资源装配、图构建、执行取消、事件投影、事件缓冲、终态持久化和观测回调。

主要复杂度集中在：

- `internal/service/agent/agent_workbench_execution_service.py`
- `internal/service/agent_protocol/event_projector.py`
- `internal/core/agent_v2/execution/agent_executor.py`
- `internal/core/agent_v2/assembly/runtime_assembler.py`
- `internal/core/agent_v2/observability/run_observer.py`

## 阶段计划

### 阶段 0：建立基线（已完成）

- [x] 绘制一次运行时序图。
- [x] 记录每个步骤的输入、输出、事务边界、资源所有权和失败处理。
- [x] 列出必须保持的行为：流式事件、取消、恢复、待填表单、审核、断线重连。
- [x] 记录当前关键日志和数据库状态变化。

验收：新成员只看时序图和入口 README，就能说明一次运行的主路径。

### 阶段 1：收紧事件接口（代码完成，待环境验收）

目标：调用方直接获得可交付的 `WorkbenchEvent`。

- [x] 调整 `AgentProtocolEventProjector.project_many()`，内部完成过滤、序号和 Schema 校验；同步核对 `generated()`、`generated_state_transition()` 和 `finish_pending_messages()` 的调用点。
- [x] 减少 `dict[str, Any]` 在 Projector、Buffer、ExecutionService 之间传播。
- [x] 统一实时事件、尾批事件和终态事件的返回类型。
- [x] 删除调用方重复的 `to_public_event()`。

验收：事件缓冲不需要了解原始事件是否合法，也不需要重复序列化。

功能验证：正文和推理增量、工具事件、并行子图消息、尾批补片、custom 通道过滤、待填表单及 SSE 重连。保留源序号与公开序号的区别，以及增量合并后游标允许跳号的规则。

### 阶段 2：收拢终态处理（代码完成，待环境验收）

目标：执行服务只负责执行流程，不拼装协议字段。

- [x] 优先扩展现有 `agent_protocol` 投影函数，集中构建生命周期、公开状态和待填表单；数据库读取、错误日志和业务状态决策保留在 Service。
- [x] 精简 `_prepare_result_status()` 与 `_build_final_state()`，不因两者都在结束阶段执行就强行合并。
- [x] 保留 Run、消息、快照和终态事件的原子提交。
- [x] 保留提交后通知订阅者的顺序。

验收：`agent_workbench_execution_service.py` 不再直接构造大部分终态协议数据。

功能验证：完成、失败、取消、等待填写、恢复后完成；覆盖尾批写入失败、终态提交失败、提交后通知失败和执行租约失效。旧执行实例不能覆盖新结果，已有答案历史不能丢失。

### 阶段 3：简化执行器接口（代码完成，待环境验收）

目标：隐藏 LangGraph `Command`、`RunControl` 和事件流细节。

- [x] 沿用 `AgentExecutor.execute()` 和 `close()`，消息与恢复答案显式二选一，取消入口改为业务语义的 `request_cancel()`，不新增 Runner 类。
- [x] 将恢复输入转换为 `Command(resume=...)` 的逻辑留在 Core；微信入口也使用统一执行结果，原生事件流不再对外暴露。
- [x] 数据库取消轮询、Flask Session 和执行租约留在 Service；Core 只消费取消信号并负责图排空，不新增仅转发控制操作的模块。
- [x] 保持 `AgentExecutionResult` 字段和状态优先级，并在 Core README 记录契约。

验收：Service 不需要知道图是如何 stream、resume 或 drain 的。

### 阶段 4：整理 Runtime 装配

目标：明确资源所有权，减少装配器中的交叉逻辑。

- [ ] 复核现有 Workspace、Skill、Memory、Sandbox、Composite Backend 工厂和构建函数，仅处理仍然重复的资源获取逻辑，不再为每种资源增加 Provider 类。
- [ ] 保留一个唯一的 `AgentRuntimeResources` 所有权对象。
- [ ] 复核构造失败、执行结束和清理失败的处理；保留清理只尝试一次、失败持续可见以及原始执行异常优先的规则。
- [ ] 明确共享 Store、Checkpointer 与单次运行 Backend 的生命周期差异。

验收：任何资源的创建和关闭都能在一个位置找到对应关系。

### 阶段 5：整理 Middleware

按用途分组：

- 业务约束：`user_input`、`review`、`tool_call_validation`
- 运行策略：重试、调用限制
- 观测：上下文窗口快照（当前实现用于估算和展示，不负责上下文压缩）
- 基础设施：Skills、Memory、Runtime Context

- [ ] `deep_agent_factory.py` 只负责组装 DeepAgents 参数。
- [ ] 每个 Middleware 只保留一个清晰职责。
- [ ] 核对默认中间件与平台扩展的职责和执行顺序；只有确认行为重复且无授权、恢复用途后才删除。

验收：普通策略调整局限于装配与对应中间件。涉及新公开事件或持久化状态的能力，明确记录必要的跨层改动；保留图状态写入审核与事件公开审核的不同职责。

### 阶段 6：压缩观测复杂度

- [ ] 保留 LangChain Callback 的自动传播和现有阶段、结束、快照入口，不改为业务层手动调用模型或工具生命周期方法。
- [ ] 将 Span、token、回调元数据合并隐藏在 Observer 内部。
- [ ] 核对每类观测数据的真实消费者。
- [ ] 核对前端、分析报表、SDK 和持久化历史后，再清理确认无用途的字段、事件和兼容结构；保留恢复执行的用量合并与去重规则。

验收：普通执行流程不需要了解 Observer 内部 Span 和快照结构。

### 阶段 7：服务层职责收敛

- [ ] `AgentAssemblyService` 只负责平台配置解析和能力授权。
- [ ] `AgentWorkbenchRunExecutionService` 只负责调度和运行结果交接。
- [ ] `AgentWorkbenchRunService` 只负责 Run、事件和租约持久化。
- [ ] Service 不直接构造 DeepAgents 内部对象。

验收：平台服务依赖 Core 的稳定入口，不依赖其内部模块。

### 阶段 8：清理迁移遗留

- [ ] 仅删除确认无调用的旧实现和重复适配器，不能因名称含 LangGraph 就认定为迁移遗留。
- [ ] 删除只转发一个对象的浅层包装模块。
- [ ] 删除同一事件的重复转换和校验。
- [ ] 更新相关 README 和阅读入口；仅在领域术语发生变化时更新 CONTEXT。

验收：代码搜索中不再出现已废弃的运行模型和重复概念。

## 每阶段验收规则

1. 理解一次普通执行所需的跨文件跳转和约定是否减少，不以文件数或代码行数作为唯一标准。
2. 调用方是否少了解一个内部概念。
3. 资源关闭和事务边界是否仍然清楚。
4. 流式输出、取消、恢复和待填表单行为是否保持一致。

按仓库约定不新增测试文件。每阶段执行适用的静态检查，并提供人工功能验证步骤；功能验证通过后再进入下一阶段。无需数据库结构变更，不执行迁移。每个提交保持可独立回退，若发现行为差异，先定位本阶段改动再继续。

## 推荐提交顺序

1. `docs: document agent runtime baseline`
2. `refactor(protocol): return validated public events`
3. `refactor(agent): centralize terminal result projection`
4. `refactor(agent): hide graph control behind runner interface`
5. `refactor(agent): simplify runtime resource assembly`
6. `refactor(agent): group middleware policies`
7. `refactor(observability): narrow observer interface`
8. `refactor(agent): remove migration leftovers`

## 第一批实施记录（2026-09-12）

- 基于 `ccc58f3` 收紧现有投影方法，统一输出 `WorkbenchEvent`，没有新增并行的 public 方法或事件包装层。
- 删除 Buffer、ExecutionService、取消和重新填写路径中的重复事件转换；数据库历史读取仍保留过滤和校验。
- 在既有 `state_projector.py` 增加执行结果与生命周期投影；删除执行服务的 `_build_final_state`，保留数据库读取、错误处理和原子提交职责。
- 对比基线与改造后的内存行为：21 组事件场景、198 组终态比较、7 组缓冲和尾批失败比较通过。故障路径使用内存替身模拟，未操作真实数据库或外部模型。
- 已通过 5 个改动 Python 文件的语法解析及相关服务导入检查；未新增测试文件。
- 实际浏览器、数据库、模型、沙箱与并发租约行为仍需按运行基线中的人工清单验收。完成验收后再评估执行器与资源装配的后续改造。

## 第二批实施记录（2026-09-12）

- 基于 `93fe9d1` 实施阶段 3。工作台传入消息或恢复答案，微信入口直接消费回答、用量和错误；不再由 Service 操作原生图流。
- 事件接收方与取消信号可选；无订阅者时继续审核输出，但不缓存事件。保留单次执行、幂等关闭和原始错误优先的行为。
- 移除已无调用的 `langgraph_conversation_config.py`，图配置直接从可信会话身份生成；更新其文档引用，未新增兼容转发接口。
- 216 组执行器差分场景与基线一致；4 组非法输入、2 组只取结果场景通过；工作台交接/监控失败与微信结果保存共 12 组内存替身场景通过。
- 使用真实 LangGraph v3 和 InMemorySaver 验证暂停/恢复、同一会话 checkpoint、用户消息不重复、单次执行约束、协作取消和一次资源关闭。未调用外部模型或操作业务数据库。
- 独立取消补测发现只请求图排空可能仍返回 completed；`request_cancel()` 现同时设置本次取消信号。修正后共享/内部信号与取消时回调失败共 4 组场景通过，并复验只取结果及真实 checkpoint 暂停/恢复。
- 改动文件通过语法与服务导入检查，未新增测试文件。真实环境继续验证多表单恢复、运行中取消、沙箱清理失败及微信兼容入口。
