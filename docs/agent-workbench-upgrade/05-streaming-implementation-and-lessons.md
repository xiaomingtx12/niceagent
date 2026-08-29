# Agent 工作台流式架构升级与运行原理

## 1. 这次改造解决的是什么问题

旧版聊天已经能够逐字输出，也能展示工具、Todo 和子 Agent 的部分过程。真正的问题不在
“能不能流式返回”，而在这条流只能服务一次 HTTP 对话请求。任务一旦变长，开始出现工具
并发、子 Agent、页面刷新、跨进程订阅和主动取消，原来的事件链就缺少稳定的运行边界和恢复
依据。

这次改造把工作台从“请求内响应式事件”升级为“线程内可恢复的 Run 事件系统”。改造后，一次
Agent 执行有独立的 `run_id` 和状态，浏览器连接只是订阅者。连接断开不会自动抹掉 Run，重新
进入页面可以通过 state 恢复快照，再从事件游标继续接收增量。

这里有两个边界需要先说清：

- 项目仍然直接使用 Deep Agents 和 LangGraph，没有自己实现 Agent 图运行时；
- 项目没有部署官方 LangGraph Agent Server，而是在现有 Flask 服务中实现了工作台需要的
  Agent Protocol HTTP Adapter。

换句话说，框架负责运行 Agent，项目负责把框架事件接进既有账户、应用、会话、数据库和
部署体系。

## 2. 改造前的流式机制

### 2.1 一次请求是怎么跑起来的

旧版调试和 WebApp 对话共用 `AgentInvocationService.stream()`。链路如下：

```text
POST /chat
  -> Handler 校验请求
  -> AppService / WebAppService 创建 Conversation 和 Message
  -> AgentInvocationService 构造一次性 BaseAgent
  -> BaseAgent.stream()
  -> 后台线程执行 LangGraph stream(messages, updates)
  -> Translator 把框架输出翻译为 QueueEvent / AgentThought
  -> AgentQueueManager 写入本进程 Queue
  -> 当前 HTTP 请求从 Queue 取事件
  -> Service 拼成 SSE 文本
  -> compact_generate_response 原样输出
```

`BaseAgent.stream()` 会生成一个临时 `task_id`，为它创建 `queue.Queue`，然后启动后台线程执行
LangGraph。执行驱动器消费 `messages` 和 `updates` 两种 stream mode，项目的 Translator 与
Middleware 再把框架输出转换成以下业务事件：

```text
agent_message
agent_action
todo_update
sub_agent_delegate
sub_agent_end
dataset_retrieval
agent_end
ping / stop / timeout / error
```

这些事件最终都变成 `AgentThought`。生产线程调用 `AgentQueueManager.publish()` 写入本进程
队列，HTTP 请求所在的生成器调用 `listen()` 逐条读取。队列一秒没有数据时，监听器检查停止
标记；十秒左右没有业务事件时补一个 `ping`；运行超过十分钟则发布 `timeout`。

### 2.2 Redis 在旧链路中的真实职责

旧链路虽然使用了 Redis，但 Redis 没有保存流式事件。它只维护两类带 TTL 的键：

```text
generate_task_belong:{task_id}  -> task 属于哪个 account/end-user
generate_task_stopped:{task_id} -> 是否请求停止
```

事件正文只存在 `AgentQueueManager._queues` 里的本地 `Queue`。因此旧机制更准确的描述是：

```text
框架流 -> 自定义事件 -> 本进程内存队列 -> SSE
                         |
                         +-> Redis 只辅助任务归属和停止标记
```

Redis 在这里解决的是跨请求停止，不是事件持久化、事件广播或断线重放。换一个 Gunicorn worker
无法读取原 worker 的内存队列，页面断开后也没有游标可以补回遗漏事件。

### 2.3 SSE 是怎样生成的

旧 Service 已经把事件编码成完整的 SSE 文本：

```python
yield f"event: {agent_thought.event.value}\ndata:{json.dumps(data)}\n\n"
```

`compact_generate_response()` 只负责把这个文本生成器交给 Flask：

```python
return FlaskResponse(
    stream_with_context(generate()),
    status=200,
    mimetype="text/event-stream",
)
```

它是一个传输包装器，不负责 Agent 执行、事件语义、游标、恢复或取消。这个函数没有失效，旧
`/chat` 接口仍可以继续使用它；只是工作台需要的能力已经超出它的职责。

### 2.4 旧机制的缺陷

#### 执行和连接绑在一起

生产线程虽然在后台运行，但它由当前请求创建，事件也只能由当前请求的本地队列消费。生成器
被关闭时会写停止标记并等待线程退出。浏览器连接、事件消费和 Agent 执行实际上仍是一组
资源生命周期。

这会带来几个直接后果：刷新页面很难重新加入正在运行的任务；网络断开和用户取消容易混成
一个动作；同一任务也无法被多个页面稳定观察。

#### 自定义事件丢失框架结构

LangGraph 原始输出先经过 Translator 才能变成 `QueueEvent`。工具、Todo、子 Agent 和模型
消息都要靠项目代码重新识别、关联和补字段。框架一旦增加新 channel 或调整嵌套结构，项目
必须继续扩充枚举和翻译规则。

`sub_agent_delegate` 之类的扁平事件只能表达项目已经猜出来的语义，无法完整保留 LangGraph
原生 `namespace`。嵌套子图越深，这种反向推断越脆弱。

#### 内存队列不能支撑多实例和重放

队列没有持久游标，也不共享。消费者只能从创建连接之后开始读取，已经出队的内容无法再次
获取。Redis 停止标记可以被其他 worker 看到，但其他 worker 看不到事件本身。

#### 缺少独立 Run 模型

旧链路主要依靠 `task_id`、Message 和最终 AgentThought 判断运行情况，没有独立、持久化的
Run 状态。“正在排队”、“运行中”、“已完成”、“已失败”和“已取消”无法作为统一业务对象被
查询和控制。

#### 消息历史只能恢复结果，不能恢复过程

旧消息接口可以重新取得用户问题和最终回答，但工具执行、Todo 变化和子 Agent 生命周期不是
一份可按游标恢复的事件历史。页面刷新后，工作台只能重建聊天结果，不能可靠还原本轮执行
过程。

#### 前端需要维护一套项目专用消费者

前端按 `QueueEvent` 名称解析 SSE，再自行合并消息分片、工具和状态。这套模型与 LangGraph
官方前端 SDK 不兼容，后续接入 interrupts、subgraph 或新的 stream channel 都要重复造轮子。

## 3. 新架构的选择

工作台需要的不只是低延迟输出，还要同时满足四类要求：

- Run 可以在没有浏览器连接时继续执行；
- 页面刷新后能恢复消息、Todo、文件和执行过程；
- 多个 Flask worker 能订阅同一 Run；
- 前端尽量使用官方 SDK 的事件和状态投影。

据此，当前实现采用以下组合：

| 层次 | 选用能力 | 负责什么 |
| --- | --- | --- |
| Agent Harness | Deep Agents | Todo、文件系统、工具、Skill、Memory、SubAgent |
| 图运行时 | LangGraph | 图执行、state、checkpointer、thread、原生事件流 |
| 运行事件源 | `stream_events(version="v3")` | `messages/values/tools/lifecycle` 与 namespace |
| HTTP 契约 | Flask Agent Protocol Adapter | commands、stream、state 和 cancel 路由 |
| 前端运行时 | `@langchain/vue` | `useStream`、`useChannel`、消息与状态投影 |
| 权威业务状态 | PostgreSQL | Conversation、Message、AgentRun、AgentRunEvent |
| 实时共享层 | Redis | Stream 重放、Pub/Sub 通知、跨进程取消 |
| 本进程加速 | `deque + Condition` | 同 worker 的低延迟事件消费 |

### 3.1 为什么选择 Event Streaming v3

旧兼容链路调用的是：

```python
graph.stream(
    input,
    stream_mode=["messages", "updates"],
)
```

工作台调用的是：

```python
graph.stream_events(
    input,
    version="v3",
)
```

v3 的每条原始事件都有统一信封：

```python
class ProtocolEvent(TypedDict):
    seq: int
    method: str
    params: ProtocolEventParams


class ProtocolEventParams(TypedDict):
    namespace: list[str]
    timestamp: int
    data: Any
```

`method` 表示 channel，`namespace` 表示事件来自根图、工具还是嵌套子图，`seq` 用于排序。
工作台可以直接观察消息、状态、工具和生命周期，不再先压成 `QueueEvent` 再猜回执行结构。

### 3.2 v2 和 v3 不是同一条版本轴

官方文档中的两个版本号容易混淆：

| 写法 | 含义 |
| --- | --- |
| `graph.stream(..., version="v2")` | LangGraph stream mode 的统一输出格式，返回 `type/ns/data` |
| `graph.stream_events(..., version="v3")` | Event Streaming 运行事件，返回 `seq/method/params` |
| Agent Protocol v2 SSE endpoint | 浏览器与 Agent Server/Adapter 之间的 commands、state、event stream 网络协议 |

当前后端事件源使用 Python Event Streaming v3；`@langchain/vue` 使用的是 Agent Protocol
前端运行时。网络协议文档中出现 v2，不代表 Python 后端退回了 `stream(version="v2")`。
这三处版本号描述的是不同层。

当前安装的 `langgraph==1.2.9` 仍可能输出：

```text
LangChainBetaWarning: The v3 streaming protocol on Pregel is experimental
```

这表示该版本实现仍保留实验性标记，不表示调用了私有接口。项目把原始事件统一收口到
`AgentProtocolEventProjector`，正是为了避免上游字段调整直接扩散到数据库和前端。

### 3.3 为什么保留 Flask Adapter

官方 Agent Server 本身可以提供 thread、run、stream、state、history、cancel 和部署能力。
当前项目没有启动 `langgraph dev` 或独立 Agent Server，也没有 `langgraph.json`。LangGraph
和 Deep Agents 作为 Python 库直接运行在 Flask 进程里。

保留 Flask 的原因来自现有业务边界：Account、App、WebApp token、EndUser、Conversation、
Message、附件和 Workspace 都已经由项目管理；每次运行还要根据应用配置动态装配模型、工具、
Skill、Memory 和 SubAgent。直接切到 Agent Server 需要重新设计网关鉴权、配置下发、数据
归属和结果回写，不是简单替换一个路由。

当前方案因此只实现工作台需要的 Agent Protocol 子集。它兼容前端 SDK，但不等于完整复制了
官方 Agent Server。assistants 管理、deployment、官方运行队列和完整 history API 并没有因此
自动获得。

## 4. 新架构的职责边界

### 4.1 五类接口各自做什么

```text
commands -> 创建并启动一次 Run
stream   -> 订阅 Run 产生的事件
state    -> 首次恢复快照、首屏消息和可见执行事件
messages -> 使用消息游标向前翻页
cancel   -> 请求取消当前活动 Run
```

`commands` 和 `cancel` 是控制面，`stream` 是数据面，`state` 是首次恢复面，`messages` 是历史
分页面。SSE 断开只影响数据订阅，不应该暗中改变 Run 状态；只有 cancel 才有权请求终止执行。

### 4.2 四类标识不能混用

| 标识 | 生成位置 | 作用 |
| --- | --- | --- |
| `thread_id` | 已有会话使用数据库 ID；新 WebApp 会话由前端 SDK 先生成 UUID | 长期任务会话与 checkpoint 归属 |
| `run_id` | 后端 command 阶段生成 | thread 内的一次 Agent 执行 |
| `event_id` | `AgentProtocolEventProjector` 生成 | 单个浏览器事件的稳定标识 |
| `seq` | 后端按 thread 连续编号 | 排序、去重和断线重放游标 |

一个 thread 可以有多次 Run。前端生成新 thread UUID 只是为了让第一次 command 已经有稳定
URL；后端仍会校验应用和用户，并在事务中创建对应 Conversation。它不是可信的权限边界。

传给 LangGraph Checkpointer 的 thread key 还会包含账号、应用、Conversation 和 Agent 维度，
防止不同业务主体仅凭相同 UUID 共享 checkpoint。

### 4.3 各存储保存什么

| 存储 | 内容 | 是否权威 |
| --- | --- | --- |
| LangGraph Checkpointer | 图 state、checkpoint、下一执行节点 | Agent 线程状态权威 |
| `conversation/message` | 用户问题、最终回答、附件和用量 | 产品对话权威 |
| `agent_run` | Run 状态、最新 values、最后 seq、错误摘要 | Run 业务状态权威 |
| `agent_run_event` | 工具和生命周期增量事件 | 历史执行展示和数据库降级 |
| Redis Stream | 短期完整事件，包括消息增量 | 低延迟断线重放，非权威 |
| Redis Pub/Sub | thread 出现新 Run 的通知 | 瞬时唤醒，非权威 |
| Redis cancel key | 跨 worker 取消信号 | 加速信号，数据库状态仍兜底 |
| 本进程 `deque` | 当前 Run 最近 2000 条事件 | 同 worker 加速，非权威 |

这些数据不能互相替代。Checkpoint 不是浏览器事件日志，Message 不是运行状态，Redis Stream
也不是长期数据库。

## 5. 一次对话的完整运行过程

### 5.1 前端提交 command

`useStream.submit()` 先生成或沿用 `thread_id`，再通过 `HttpAgentServerAdapter` 调用：

```text
POST /apps/{app_id}/threads/{thread_id}/commands
POST /web-apps/{token}/threads/{thread_id}/commands
```

command 使用 `run.start` 语义，并携带用户消息、附件和消息 ID。后端执行以下步骤：

1. Handler 只读取 HTTP 参数，交给 `AgentWorkbenchService.start_run()`；
2. Schema 校验 `run.start` 和 input 结构；
3. Service 校验 App/WebApp、账号、EndUser 与 thread 归属；
4. 通过 `_starting_runs` 和输入消息 ID 阻止重复启动；
5. 使用 PostgreSQL 事务级 advisory lock 串行化同一 thread 的 Run 创建；
6. 使用 `thread_id + input_message_id` 查询已接受的 Run，跨进程重试直接返回原 `run_id`；
7. 在同一事务中创建或确认 Conversation、创建 Message 和 `AgentRun`；
8. 数据库部分唯一索引保证同一 thread 最多只有一个 `queued/running` Run；
9. 读取 thread 已发布的最大 seq，作为新 Run 的起始序号；
10. 提交事务后，把 Run 放入本进程注册表并发布 Redis 新 Run 通知；
11. 启动带 Flask application context 的后台生产线程；
12. 返回 `run_id` 和 `applied_through_seq`。

command 返回只代表 Run 已经被接受，不代表 Agent 已完成。

### 5.2 后台生产者执行 LangGraph

后台线程先加载完整应用配置，再通过 `AgentAssemblyService.build_agent()` 动态装配 Deep
Agents。随后调用：

```python
agent.stream_events({"messages": [query]})
```

`AgentExecutor.stream_events()` 内部直接消费：

```python
self._agent.stream_events(
    {"messages": messages},
    config=effective_config,
    context=self._runtime_context,
    version="v3",
)
```

Deep Agents 负责 Todo、工具、文件和 SubAgent，LangGraph 负责执行图、维护 state、写 checkpoint
并产生事件。Flask Service 不参与决定下一节点，也不维护另一套 Todo 或子 Agent 状态机。

运行配置中的 `configurable.thread_id` 由服务端 `AgentRunContext` 构造。LangGraph 在图的
super-step 边界把 channel values、下一节点和运行 metadata 写入 Checkpointer。下一轮调用只要
使用同一个稳定 thread key，就会从该线程最近的 checkpoint 继续，而不是把完整历史重新拼进
一次请求。业务 Conversation 决定产品层会话归属，LangGraph thread key 决定运行时状态隔离，
两者有关联，但职责不同。

### 5.3 原始事件怎样变成浏览器事件

事件经过两层投影：

```text
LangGraph v3 raw event
  -> AgentProtocolEventProjector
  -> sanitize_workbench_event
  -> PostgreSQL / Redis / memory buffer
  -> SSE
```

`AgentProtocolEventProjector` 的工作包括：

- 从 thread 上一 Run 的最后 seq 继续编号；
- 生成稳定的 `event_id`；
- 保留 `method`、`namespace`、`timestamp` 和框架原始 `source_seq`；
- 把 `AIMessageChunk` 展开为 `message-start`、文本 delta 和 `message-finish`；
- 在框架没有提供完整终止分片时按 namespace 关闭尚未完成的消息流；
- 补充 Run 的 started/completed/failed 生命周期事件，并用
  `interrupted + status=cancelled` 表达应用取消。

安全投影只公开 `messages`、`values`、`tools` 和 `lifecycle`，递归删除 token、Authorization、
system prompt、数据库连接和异常栈等敏感字段。`values` 只保留 messages、todos 和 files。
投影发生在事件写入 Redis 或数据库之前，敏感数据不会先进入共享存储再由前端隐藏。

### 5.4 事件怎样落入不同存储

每条可见事件先更新 Run 的 seq 和 values，然后依次写入：

```text
Redis Stream -> AgentRun/AgentRunEvent -> 本进程 deque -> Condition 唤醒
```

消息 token 增量数量大，只保留在短期 Redis Stream 和内存缓冲中；最终 messages 由 values
快照和业务 Message 持久化。`values` 只覆盖 `agent_run.state_snapshot`，不再追加到事件表；
工具和生命周期增量写入 `agent_run_event`，供 state 和 Redis 故障时恢复。

正常完成时，Service 从最终 values 中提取 Agent 回答，更新 Message 的 answer、latency 和
token 用量，再发布一份包含当前消息页的终态 values，最后发布 lifecycle completed。这样
最终快照、业务消息和 Run 状态会在运行结束时收敛。

## 6. stream 订阅和断线重放

### 6.1 stream 不负责启动 Agent

前端调用：

```text
POST /apps/{app_id}/threads/{thread_id}/stream
POST /web-apps/{token}/threads/{thread_id}/stream
```

请求体可以携带：

```json
{
  "channels": ["messages", "values", "tools", "lifecycle"],
  "namespaces": [[]],
  "depth": 1,
  "since": 128
}
```

Handler 完成鉴权和响应封装，Service 解析 `WorkbenchSubscription`。如果 Run 位于当前 worker，
订阅本进程 `deque + Condition`；如果 Run 在其他 worker，使用 Redis Stream 的阻塞读取；Redis
不可用时才低频查询 PostgreSQL。

stream 会在 Run 之间继续等待。Redis Pub/Sub 只通知“thread 有新 Run”，订阅者收到 run_id 后
再从本地缓冲或 Redis Stream 读取事件，避免每条长连接持续轮询数据库。

### 6.2 重连怎样恢复

客户端把最后处理的 seq 放进 `since`，服务端只返回更大的事件。Redis Stream 如果仍保留该
游标，就先重放遗漏事件，再继续阻塞等待新事件。

当 Redis Stream 已裁剪掉旧游标时，服务端发布带 `cursor_reset` metadata 的 values 事件，
客户端以可用快照重新对齐。Redis 完全不可用时，非 token 事件和最新 values 仍可以从
PostgreSQL 恢复。

### 6.3 为什么一次 command 会看到多条 stream

一条 `/commands` 只创建一个 Run，多条 `/stream` 是多个只读订阅，不会重复执行模型。
当前 `@langchain/vue`/SDK 会建立：

1. 根内容订阅，读取 messages、values、tools、lifecycle 和 input；
2. 全 namespace 的 lifecycle/input 订阅，用于发现任意深度的 SubAgent 和 interrupt；
3. 只有显式使用 `useChannel(..., { replay: true })` 时，才会再建立 channel 重放订阅。

当前组件已经通过 state 的 `workbench_events` 恢复历史，因此使用 `replay: false` 共享实时流，
不再为相同的 values/tools/lifecycle 历史额外建立第三条订阅。SDK 仍可能为根内容和全 namespace
生命周期分别建立连接，这两条订阅用途不同。

排障时应检查请求体。如果三条连接的 channel/namespace 不同，这是 SDK 的订阅拆分；如果
相同请求依次关闭并重开，则是 reconnect 或 idle reconnect，需要检查心跳、代理缓冲和客户端
AbortController。

### 6.4 SSE 响应封装

工作台 Handler 使用：

```python
return sse_response(service.authorized_stream(...))
```

Service 返回结构化事件，`sse_response()` 在 HTTP 边界统一 JSON 编码，并设置：

```text
Content-Type: text/event-stream
Cache-Control: no-cache, no-transform
X-Accel-Buffering: no
```

连接建立时先发送 `: connected`，空闲时发送 `: heartbeat`。这与旧
`compact_generate_response()` 的输入契约不同：旧函数接收已经编码好的 SSE 文本，工作台
响应函数接收结构化字典。两者不能混用，否则会出现重复 JSON 编码或错误的 SSE 分隔。

### 6.5 前端为什么能响应式更新

`HttpAgentServerAdapter` 把 SDK 的 command、stream 和 state 动作映射到项目的 Flask 路由。
SSE 帧进入 SDK 后，不由 Vue 组件逐条解析，而是先交给 `StreamController`：

```text
Flask SSE
  -> HttpAgentServerAdapter
  -> StreamController
  -> root store / channel registry
  -> useStream.values、messages、isLoading
  -> useChannel(values/tools/lifecycle)
  -> Vue computed
  -> 对话、Todo、工具和 Run 详情
```

`useStream` 维护 thread、消息、values、loading、error 和提交生命周期；`useChannel` 从共享事件
仓库筛选指定 channel，并提供有界缓冲。组件把 state 返回的 `hydratedEvents` 与实时
`rawEvents` 按 `event_id` 去重，再按 seq 排序，最后投影成每轮对话关联的 Run 执行记录。

发送消息时启用 optimistic projection，用户输入先进入本地消息列表。后端沿用同一个 message
ID 返回确认事件后，SDK 按 ID 合并，不会追加一条重复用户消息。这样输入和停止操作可以立即
反馈，服务端事件随后负责最终一致性。

## 7. state 和历史消息

### 7.1 state 的用途

首次进入 thread 时，`useStream` 调用：

```text
GET /.../threads/{thread_id}/state
```

响应包含：

```text
values.messages         当前一页业务消息
values.todos            最新 Todo 快照
values.files            最新文件快照
values.workbench_events 当前消息页关联 Run 的执行事件
values.message_page     历史分页游标
next                    Run 是否仍有后续节点
metadata                run_id/status/last_event_sequence
```

state 是前端 hydration 快照，不是 LangGraph Checkpointer 的直接 HTTP 暴露。Service 会组合
Message、AgentRun、AgentRunEvent 和最新 values，输出浏览器需要的安全结构。

### 7.2 历史滚动

历史滚动使用独立的消息游标端点：

```text
GET /.../threads/{thread_id}/messages?before={message_id}&page_size=20
```

`state` 不再承担翻页。它只在 SDK 初始化时读取首屏消息及这些消息关联的 Run，不扫描 thread
的全部 Run。前端滚动到顶部时调用 `messages`，该接口只查询一页业务 Message，不重复装配
values 和执行事件，因此延迟和查询量都更稳定。

state 没有取代数据库 Message，也没有取代 LangGraph checkpoint。它只是把几种权威数据投影
成一次可供 SDK 恢复的快照。

## 8. 取消运行

当前提供一条取消路径：

```text
POST /{scope}/threads/{thread_id}/runs/{run_id}/cancel  -> 取消指定 Run，返回 JSON
```

`run_id` 直接来自 `run.start` 的 command 响应，不依赖 SSE 的 `run.started` 事件，因此不存在等待首个
流事件才能取消的竞态，也不会误取消同一 thread 上刚创建的下一轮 Run。

取消是协作式的：

1. 数据库先把 AgentRun 标记为 `cancelling`；
2. Redis 写入跨进程 cancel key；
3. 同 worker 设置 `threading.Event`；
4. 生产者在下一个原生事件边界检查取消信号；
5. 投影 `lifecycle.interrupted`，同时携带应用状态 `cancelled`，释放运行资源后再把 Run 写入
   `cancelled` 终态。

Python 线程和正在执行的外部工具不能被安全强杀，所以 cancel 的响应成功不等于每个底层调用
都在同一毫秒停止。模型或工具返回到下一个事件边界后，生产者才会收敛。

前端点击停止时直接调用官方 SDK 的 `stream.stop()`。SDK 立即结束本地 loading 和订阅，并使用当前
`thread_id + run_id` 请求服务端取消。输入框不因后台运行而禁用，用户可以继续编辑下一条内容；
后端仍通过 `cancelling -> cancelled` 协作式收敛。

### 8.1 取消后立即提交下一轮

取消接口成功时，`AgentRun` 进入 `cancelling`，但旧生产线程可能还在等待模型或工具返回。
此前 command 只检查本地 Run 的 `done` 事件，因此在“取消状态已写入、生产线程尚未退出”的窗口
内再次提交，会错误返回“当前任务已有正在执行的 Agent”。

现在同一 thread 采用串行排队语义：

1. 旧 Run 收到取消请求后，新的 command 可以立即创建一个 `queued` Run 并返回新 `run_id`；
2. 同进程使用 `done` 等待，跨进程通过数据库检查前序未终止 Run；
3. 旧 Run 发布 `lifecycle.cancelled`、释放资源并进入终态后，新 Run 才允许执行；
4. 新 Run 启动前重新读取 thread 的最大 `last_event_sequence`，避免终态事件导致序号重复；
5. 如果新 Run 在等待期间也被取消，它在前序 Run 收敛后直接发布 cancelled，不初始化 Agent。

不能在取消接口中直接把旧 Run 的 `done` 设为完成。协作式取消尚未收敛时，旧 LangGraph 执行仍
可能写入 checkpointer；若新 Run 同时使用相同的 `thread_id` 启动，两次执行会并发修改同一份
持久化状态。排队既消除了前端重试报错，也保留了一个 thread 内最多一个图执行的约束。

## 9. Flask、线程与数据库资源原理

WSGI 响应体是一个可迭代对象。Handler 返回后，SSE Generator 仍可能存活很久；客户端断开、
垃圾回收或热重载都可能在请求上下文销毁后才触发 `finally`。

这次出现过以下异常：

```text
RuntimeError: Working outside of application context
```

原因是在生成器关闭阶段调用 `self.db.session.remove()`，Flask-SQLAlchemy 为了解析 scoped
session 又访问了已经不存在的 application context。

当前处理分成两种情况：

- command 启动的后台生产线程显式进入 `flask_app.app_context()`，线程结束时再 `remove()`；
- SSE 生成器在上下文仍有效时取得具体 Session，关闭阶段直接调用该实例的 `close()`，不再
  重新解析 `current_app` 或 scoped proxy。

所有只读鉴权、状态查询和取消轮询也在使用后立即关闭 Session，避免每条 SSE 长连接长期占用
数据库连接池。

Windows 下 Werkzeug reloader 还可能在关闭监听 socket 时输出 `WinError 10038`。若日志随后
出现 `Restarting with stat` 并正常启动，这是本地热重载竞态，不是 Agent Run 或 SSE 协议异常。
生产环境不应使用 Werkzeug debug reloader。

## 10. Redis 与数据库的降级关系

| 场景 | 行为 |
| --- | --- |
| 当前 worker 执行 Run | 从本进程有界缓冲读取，延迟最低 |
| 其他 worker 执行 Run | Redis Stream 阻塞读取和重放 |
| thread 出现下一轮 Run | Redis Pub/Sub 唤醒订阅者 |
| Redis Stream 游标过期 | 发 cursor reset values，客户端回到快照 |
| Redis 不可用 | PostgreSQL 事件和 Run 状态低频轮询兜底 |
| 浏览器断开 | 关闭 SSE、Pub/Sub 和 Session，Run 默认继续 |
| 服务进程退出 | 内存缓冲消失；已提交的数据库状态仍在，但当前自建后台线程不会自动迁移到其他 worker |

最后一项是当前嵌入式方案的重要限制。AgentRun 和事件持久化能够保留已发生的状态，但 Flask
进程被强制终止时，项目没有官方 Agent Server 那样的持久运行队列自动接管未完成 Run。若要
获得跨进程故障续跑，需要把生产者迁入可靠任务队列，或正式接入 Agent Server，而不是继续
增加 SSE 层逻辑。

## 11. 代码分层如何落地

这次整理后，各包只保存自己拥有的概念：

```text
internal/handler
  -> HTTP 参数、登录边界、调用 Service、响应封装

internal/schema
  -> *Req 请求、*Resp 响应、订阅参数和 Redis 重放批次

internal/entity
  -> WorkbenchRun、WorkbenchRunInput、WorkbenchRunStatus、启动占位值对象

internal/repository
  -> Redis Stream、Pub/Sub、取消 key 的协议和故障降级

internal/service
  -> Access 鉴权、History 查询、RunFactory 构造、Run 持久化和流式编排

internal/exception
  -> WorkbenchReplayCursorExpired

internal/model
  -> AgentRun、AgentRunEvent 数据库模型

pkg/response
  -> json_response、sse_response、旧 compact_generate_response
```

Handler 不拼 SSE、不解析 Agent 事件，也不查询业务状态。Schema 负责外部协议校验，Entity
表达运行期领域状态，Service 负责编排数据库、Redis 和 Agent，Projector 负责纯事件转换。

## 12. 改造前后对照

| 维度 | 改造前 | 改造后 |
| --- | --- | --- |
| 运行入口 | `/chat` 请求直接启动 | `/commands` 创建 Run |
| 事件来源 | `stream(messages, updates)` 后自定义翻译 | `stream_events(version="v3")` 原生事件 |
| 事件模型 | `QueueEvent/AgentThought` | `ProtocolEvent` 投影 |
| 执行与连接 | 同一请求生命周期内协作 | Run 与 stream 订阅分离 |
| 事件顺序 | 队列出队顺序 | thread 连续 `seq` |
| 嵌套作用域 | 自定义 agent_id/parent 字段 | LangGraph `namespace` |
| 本地实时层 | `queue.Queue` | 有界 `deque + Condition` |
| Redis | 归属键和停止标记 | Stream、Pub/Sub、cancel key |
| 持久运行状态 | Message/AgentThought 为主 | AgentRun 独立状态 |
| 断线恢复 | 不支持事件重放 | `since` + Redis Stream + state |
| 多 worker 订阅 | 无法读取其他进程 Queue | Redis Stream，数据库降级 |
| 历史恢复 | `/messages` 恢复问答 | state 首次水合，messages 独立游标翻页 |
| 取消 | 前端依赖临时 task_id | SDK run cancel，cancelling 中间态，DB/Redis/local Event |
| 前端 | 项目自定义 SSE 消费器 | `@langchain/vue` useStream/useChannel |
| SSE 编码 | Service 先拼文本 | `sse_response` 在 HTTP 边界编码 |

## 13. 当前方案仍需注意的边界

- `stream_events(version="v3")` 在当前依赖版本仍带实验性警告，升级 LangGraph 时必须核对事件
  字段和 changelog；
- 当前只公开同步 SubAgent 的事件观察，没有完整开放 `interrupt()`/
  `Command(resume=...)` 的 HITL 产品流程；
- 一条 SSE 断开不能推导 Run 已取消，组件卸载和网络中断只应释放订阅；
- state 首次 hydration 和历史翻页必须走不同查询范围，不能让滚动加载触发全量 Run 事件读取；
- Redis Stream 有 TTL 和长度上限，只用于短期重放，长期审计仍以 PostgreSQL 为准；
- SDK 可能按 channel 建立多条 stream 订阅，后端必须把订阅视为独立消费者，不能让一次断连
  终止 Run；
- 生产部署必须为长连接预留合适的 Gunicorn worker、数据库连接池、反向代理超时和禁缓冲配置。

## 14. 相关实现

后端：

- `internal/core/agent_v2/execution/agent_executor.py`
- `internal/core/agent_v2/protocol/event_projector.py`
- `internal/core/agent_v2/protocol/event_sanitizer.py`
- `internal/handler/agent_workbench_handler.py`
- `internal/service/agent_workbench_service.py`
- `internal/service/agent_workbench_access_service.py`
- `internal/service/agent_workbench_run_service.py`
- `internal/repository/agent_workbench_redis_repository.py`
- `internal/schema/agent_workbench_schema.py`
- `internal/entity/agent_workbench_entity.py`
- `internal/model/agent_run.py`
- `pkg/response/response.py`

前端：

- `src/features/agent-workbench/api/adapter.ts`
- `src/features/agent-workbench/composables/use-agent-workbench.ts`
- `src/features/agent-workbench/components/AgentConversationWorkbench.vue`
- `src/features/agent-workbench/models/event-projection.ts`

## 15. 官方资料

- [LangGraph Event Streaming](https://docs.langchain.com/oss/python/langgraph/event-streaming)
- [LangChain Event Streaming](https://docs.langchain.com/oss/python/langchain/event-streaming)
- [Deep Agents Event Streaming](https://docs.langchain.com/oss/python/deepagents/event-streaming)
- [LangGraph Streaming v2](https://docs.langchain.com/oss/python/langgraph/streaming)
- [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
- [Agent Protocol v2 Event Stream SSE](https://docs.langchain.com/langsmith/agent-server-api/streaming/protocol-v2-event-stream-sse)
- [Join and Rejoin](https://docs.langchain.com/oss/python/langchain/frontend/join-rejoin)

本文前半部分保留旧 Queue/Thread/EventSession 机制，用于说明改造前的缺陷。2026 年 8 月 26 日
后续清理已经删除该执行链、旧 SSE 兼容投影和 `task_id` 取消协议。所有 Agent 入口统一消费
Agent Protocol。当前实现和删除边界见
[旧流式执行链删除说明](./07-legacy-stream-removal.md)。工作台新功能应继续沿 command、stream、
state、messages 和 cancel 的边界演进。
