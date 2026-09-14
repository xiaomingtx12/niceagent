# Agent Service 结构诊断

本文只做**客观描述与原因分析**，不修改任何代码，也不构成改造决议。

- 对照对象：`internal/service/agent/` 与 `internal/service/workflow/`
- 对照基准：`internal/service/workflow/workflow_service.py` 被认定为"清晰"的样板
- 结论依据：当前工作区代码（行号以当前文件为准）
- 关联文档：[Agent-业务文档.md](Agent-业务文档.md)（业务语义与 §12 已知问题）

---

## 1. 结论

两边的差异不在"写得好不好"，而在**切分维度不同**：

| | 切分维度 | 结果 |
| --- | --- | --- |
| `workflow_service.py` | 按**业务动作**切 | 方法名即业务能力，读一遍就知道系统能做什么 |
| `internal/service/agent/` | 按**技术角色**切 | 文件名只表明有哪些零件，读一遍不知道系统能做什么 |

`workflow_service.py` 的阅读路径是**线性的**：一个类、一个方法、一个事务，从上到下就是执行顺序。

Agent 侧的阅读路径是**往返的**：读一个业务动作必须在多个文件之间跳转，其中一次还要跨线程边界。这是"不清晰"最主要、最直接的来源。

真正的问题不是"拆得太碎"，而是：**agent 侧的并发、异步、可重连这些真实约束，被表达成了"调用顺序约定"，而不是被表达成"结构"。约定要背，结构不用背。**

---

## 2. 对照事实

| 指标 | `internal/service/workflow/` | `internal/service/agent/` |
| --- | --- | --- |
| 业务模块数 | 4 | 13 |
| 总行数（含空行） | 1542 | 3546 |
| 单文件最大 | `workflow_service.py` 731 行 | `agent_workbench_run_service.py` 669 行 |
| 讲几件事 | 工作流 CRUD/校验/调试/发布、生成器、内置工作流、SCF 部署 | 工作台 Run 生命周期、模型、定义、装配、附件、助手、创建者任务、会话清理 |
| 入口类依赖数 | 3（`db`、`builtin_provider_manager`、`language_model_service`） | 8（`db`、`run_service`、`queue_manager`、`execution_service`、`definition_service`、`model_service`、`attachment_service`、`conversation_service`） |
| 入口类方法数 | 13（10 个业务入口 + 3 个私有） | 11（5 个业务入口 + 6 个私有） |
| 是否有阅读指南文档 | 无 | 有，`agent/README.md` 86 行 |
| 依赖图形状 | 树（service → db / 其他 service） | 网（持久化层反向依赖进程内运行时状态层） |

其中**只为实现一套 Run 生命周期**的模块有 6 个，合计 1986 行：

```
agent_workbench_service.py            430
agent_workbench_run_service.py        669
agent_workbench_execution_service.py  376
agent_workbench_query_service.py      219
agent_workbench_event_buffer.py       157
agent_workbench_event_queue.py        135
```

而 `workflow_service.py` 用 **1 个文件 731 行**覆盖了工作流的全部业务动作。

---

## 3. 六条具体差异

### 3.1 一次业务动作的阅读跨度不同

`workflow` 的 `debug_workflow`（`workflow_service.py:403`）从校验、建工具、流式消费到落库全在同一个方法体内，读一遍就完事。

Agent 侧"发一条消息"要穿过 5 个模块、6 个对象：

```
handler
 → AgentWorkbenchService.start_run                       agent_workbench_service.py:56
    → run_service.lock_conversation / get_blocking_run / add_run
    → queue_manager.register
 → AgentWorkbenchRunExecutionService.launch              agent_workbench_execution_service.py:61
    ── 此处跨线程边界，调用栈断开 ──
 → _execute_background_run:317 → execute:82
    → assembly_service.build_agent                       agent_assembly_service.py:88
    → event_buffer.receive                               agent_workbench_event_buffer.py:43
    → run_service.record_events                          agent_workbench_run_service.py:288
    → run_service.finalize_run:243 → queue_manager.notify_events
```

`workflow` 里没有任何一段需要"读到这里跳到另一个文件、再跳回来"，更没有跳到另一个线程。

### 3.2 命名几乎不携带信息

- 13 个模块中 **11 个**以 `agent_` 开头，其中 **6 个**共享更长的 `agent_workbench_` 前缀。公共前缀越长，区分度越低，真正起区分作用的只剩中间那个词。
- `AgentWorkbenchRunService`（管数据库）与 `AgentWorkbenchRunExecutionService`（管执行）从名字看不出谁依赖谁。
- 对照 `workflow` 的 4 个模块：`workflow_service` / `workflow_generator_service` / `builtin_workflow_service` / `tencent_scf_service`，各自是独立业务域，**名字即边界**。
- 同一概念被多个名字复用：`launch` / `execute` / `_execute_agent` / `_execute_background_run` 全在讲"执行"。
- 名字与实现不符：`AgentWorkbenchEventQueueManager` 不是队列（是进程内 Run 注册表）；`AgentWorkbenchEventBuffer` 既缓冲又负责刷盘与首字计时。

### 3.3 正确性靠"调用约定"维持，而不是靠结构

`workflow_service` 的数据流是**值**：字典进、ORM 行出，写操作由 `BaseService.create/update/delete` 统一收口事务。

Agent 侧的数据流是**一批可变对象**：`WorkbenchRun`（`executor_id` / `cancel_requested` / `done` / `last_event_sequence` 被多方读写）、`AgentRun` 行，外加三种快照（`invocation_snapshot` / `interaction_state` / `state_snapshot`）。由此产生大量无法用类型表达的隐含前提：

- "调用方必须先锁会话，再锁 Run"
- "提交前必须 `register`，否则并发请求会把它判成孤儿"（`agent_workbench_service.py:82`）
- "事件只能在提交后 `notify`"
- "本方法保留调用方 Session 和事务"

`agent_workbench_run_service.py` 中出现"调用方持有锁 / 调用方事务 / 由调用方负责"这类**前提声明 15 处**（第 115、134、146、157、170、187、208、223、326、374、467、485、603、626、651 行）。

也就是说：**docstring 已经变成了真正的接口**。`agent/README.md` 的 86 行"阅读入口"与"修改时的边界"就是在补偿这一点。对照之下，`workflow_service.py` 没有 README，方法注释只有"根据传递的请求信息创建工作流"这一句。

### 3.4 职责边界用"否定式"定义

`agent/README.md` 的"修改时的边界"一节几乎全是"不做什么"：

- 命令服务**不**直接构造 ORM 查询
- 查询服务只读**不**写
- 模型方法**不**查询或提交数据库
- 执行服务**不**自行拼接 psycopg 错误集合、**不**构造 LangGraph Command

否定式规则往往说明这个切面本身不自然——同一件事被切开后，切口处需要反复声明。

`workflow` 的边界是肯定式的：这个 Service 就是管 `Workflow` 表和它的 graph 校验，不需要额外声明。

### 3.5 依赖从"树"变成了"网"

- `agent_workbench_run_service.py:43,64` 反向依赖 `AgentWorkbenchEventQueueManager`（进程内线程注册表），用途在 `settle_orphan_runs:329` 读取 `active_run_ids()` 判断孤儿。**持久化层依赖运行时内存状态层**，属于分层倒置。
- `agent_assembly_service.py:76,247` 装配服务也持有 `run_service`，只为读取 `get_confirmed_input_context`。
- `agent_workbench_event_buffer.py:15,25` 事件缓冲同时持有 `run_service` 与 `queue_manager`。

`workflow_service` 的依赖是单层：service → db / 其他 service。读者可以靠"从入口往下"确定调用范围；agent 侧做不到。

### 3.6 事务归属在同一文件里有两套约定

`agent_workbench_run_service.py` 中：

- `mark_running:232`、`finalize_run:257`、`record_events:308`、`settle_orphan_runs:330`、`request_cancel:566` 各自在方法内部 `with self.db.auto_commit()`，即**隐式提交**；
- 而 `add_run:207` 明确"交给调用方事务 flush"（`:223-226`），**不提交**。

读者必须逐个记住哪个方法是哪种，才能理解调用方为什么写 `try/finally: self.db.session.close()`（如 `agent_workbench_execution_service.py:186`）。`workflow_service` 的写操作事务在 `BaseService` 里统一收口，不存在这个问题。

---

## 4. 一个症状，不是原因

`WORKBENCH_OCCUPYING_STATUSES` 在 `agent_workbench_run_service.py:151`（`get_blocking_run`）与 `:574`（`request_cancel`）被使用，但模块顶部导入块（`:17-22`）只导入了 `WORKBENCH_ACTIVE_STATUSES` / `WORKBENCH_TERMINAL_STATUSES` / `WorkbenchRun` / `WorkbenchRunStatus`，运行到这两行会抛 `NameError`。

该项**已在** [Agent-业务文档.md](Agent-业务文档.md) §12.1 记录（该处写的行号 145 / 568 已随文件增删发生偏移，现为 151 / 574）。此处只作为佐证引用：在 13 个同前缀模块 + 40 余条边界注释之后，一个顶部 import 缺失能通过阅读，说明该目录的**可扫描性**确实偏低。

---

## 5. 哪些复杂度是真实的

Agent 侧存在 `workflow` 没有的四个真实约束，**不能靠"合并成一个大文件"解决**（那只会得到 3000 行的巨型类）：

1. **执行是异步跨线程的** —— `workflow.debug_workflow` 是同步生成器，Agent 走 `ThreadPoolExecutor`（`agent_workbench_execution_service.py:56`）。
2. **同一会话存在并发竞争** —— 需要会话锁、Run 锁、执行租约（`executor_id`）三层。
3. **事件是流式且可重连的** —— 需要持久化游标（`last_event_sequence`）、独立扫描游标（`EventBatch.next_sequence`）与提交后通知。
4. **存在取消 / 待填 / 恢复三种中断语义** —— 由 `AgentRunInteraction` 集中维护，并需要与消息状态、Run 状态原子提交。

因此诊断的重点不是"减少文件数"，而是**让这些约束由结构承担，而不是由注释和调用顺序承担**。

---

## 6. 附：可选改造方向（尚未决策）

仅列出方向与代价，不代表推荐顺序，也未确定改动范围。

| 方向 | 内容 | 风险 | 收益 |
| --- | --- | --- | --- |
| A. 仅修边界与命名 | `settle_orphan_runs` 的存活集合改为调用方传入（`run_service` 不再 import `queue_manager`）；按职责重命名模块与类 | 低，不改调用链 | 消除分层倒置，降低名字歧义 |
| B. 收回命令链 | 把 `start/respond/reopen/cancel` 的持久化步骤收回 `agent_workbench_service.py` 私有方法（它们只有一个调用方），run 服务只保留被多个调用方共享的查询 | 中，改动面大 | 文件数 13 → 约 8，一次对话可在少数文件内读完 |
| C. 把"一次执行"收敛成对象 | run / queue / buffer / projector 收进 Execution 上下文，取消、事件、终态由对象生命周期保证 | 高 | 收益最大：约定变成结构，可测试性明显提升 |

方向 C 的关键判断点：`agent_workbench_event_buffer.py` 目前只有一个消费者（`agent_workbench_execution_service.py:39`），把它与 `queue_manager` 收进执行对象，是最自然的起点。
