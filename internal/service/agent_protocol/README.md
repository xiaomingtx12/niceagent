# 工作台协议适配

本目录只负责工作台业务数据与前端协议之间的映射。框架事件解析在 `internal/core/agent_v2/execution/event_normalizer.py`；内部异步子 Agent 的 SDK 接口在相邻的 `internal_agent_protocol` 目录。

## 先看整条链路

启动命令和事件订阅是两个 HTTP 请求：`/commands` 创建 Run 并交给后台执行，返回 Run ID；`/stream` 根据 Run ID 和游标恢复已保存历史，再从内存尾部持续发送实时事件。

```text
后台执行：AgentExecutor → normalizer.accept → events.receive（筛选并适配载荷）
                                             → event_projector.create_event（共用编号与协议外壳）
                                             → 内存尾部 → 通知实时订阅者
                                             → 独立写线程合批调用 _record_events → 事件表

HTTP 订阅：AgentExecutionQuery._stream_events → 已保存历史接内存尾部
                                      → sse_response 编码 → 浏览器
```

本目录只保留多处共用的协议处理：`event_projector.py` 提供事件编号、协议外壳、终态批次和历史解析，`state_projector.py` 提供图状态适配与公开快照筛选。只有一个调用方的业务步骤直接写在调用函数内，包括事件接收转换、聊天历史与待填写任务构造、执行结果状态和生命周期映射。

`AgentExecutionEventBuffer` 发布内存事件后立即唤醒订阅者，批量写入独立进行。刷新和断线重连共用“已保存前缀 + 内存尾部”，按同一序号衔接，已经发送的实时事件落库后不重复发送。图结束时先排空写队列，由 Service 保存回答、状态和终态事件，再通知前端运行已结束；服务异常退出允许丢失尚未保存的尾部。

## 阅读顺序

1. `agent_protocol_schema.AgentProtocolCommandReq.from_mapping`：一次解析命令外壳与参数，兼容 SDK 的嵌套输入及单个／批量回答。`params` 在解析后已经是 `AgentRunInput`、`AgentProtocolInputRespondReq` 或 `AgentProtocolInputReopenReq`。
2. 命令分派与成功／错误外壳由 `internal/handler/agent/entry_adapter.py` 的 `respond_command` 处理：消费已解析的参数直接调用启动、回答和重新填写入口，再包装协议响应。
3. `AgentExecutionEventBuffer.receive`：直接筛选 Core 事件、适配载荷并绑定业务消息 ID，再调用 `create_event` 分配持久化序号和协议外壳。
4. `AgentExecutionService._commit_result`：直接构造执行结果状态与待填写表单；`_prepare_result_status` 直接确定业务消息状态和公开生命周期。
5. `AgentExecutionQuery.get_state`：直接构造聊天历史、补齐活动消息并生成待填写任务。共用的图状态适配和快照白名单保留在 `state_projector.py`。

### 主要方法的处理步骤

| 方法 | 处理顺序 |
| --- | --- |
| `AgentExecutionEventBuffer.receive` | 排除框架根终态 → 适配载荷 → 编号 → 缓冲和提交 |
| `AgentExecutionQuery.get_state` | 查询历史与 Run → 构造用户和助手消息 → 补齐活动消息 → 汇总状态与表单 |
| `adapt_graph_state` | 复制输入状态 → 合并每轮 AI 回答 → 适配异步任务会话字段；其余图状态字段保留 |
| `select_public_snapshot` | 按公开字段白名单提取状态，供查询和业务状态通知使用 |
| `AgentExecutionService._commit_result` | 提取结果展示字段 → 验证中断表单 → 保留答案历史 → 原子保存结果与终态事件 |

消息合并直接写在 `adapt_graph_state` 内：用户消息开始新回合，工具与其他消息原样保留，后续 AI 回答替换本轮第一次回答的位置。深拷贝保证适配结果与输入快照隔离；`adapt_graph_state` 负责字段适配，`select_public_snapshot` 负责公开字段筛选。

## HTTP 入口

`internal/handler/agent` 按入口组织处理器：`DebugAgentHandler`、`WebAppAgentHandler`、`CreatorTaskAgentHandler` 各负责命令、订阅、状态、模型选项和取消；每个处理器只依赖自己的会话授权入口。`AgentExecutionHandler` 单独负责 Run 历史事件查询。

六个入口的命令、订阅、状态、模型选项和取消行为完全一致，差异只有会话鉴权方式和 `InvokeFrom`，因此统一由 `internal/handler/agent/entry_adapter.py` 解析请求并包装响应；每个处理器只保留一个 `_caller(...)` 完成鉴权并组装可信身份，不再各自复制协议处理。鉴权先于请求体解析，未授权请求不会触发参数校验。该适配器被工作台入口和 Agent Protocol 入口共用，因此归在 `handler/agent`，`handler/agent_protocol` 只放专属入口本身。

Handler 完成各入口鉴权后，按操作需要直接传递账号、应用、会话、创建者和固定调用来源，不构造通用 Context。状态查询只接收会话 ID 和来源，取消与订阅额外校验应用归属；启动和恢复接收完整调用身份。`AgentInvocation` 只在创建 Run 或恢复执行时保存完整输入，JSON 字段保持与历史快照兼容。路由保留原 URL、HTTP 方法和 Flask endpoint，Assistant、OpenAPI 和内部 SDK 的鉴权仍各自独立。

各入口 Service 直接查询并校验允许的会话来源和账号归属，不再将 `allowed_sources` 交给通用会话服务。Creator 的执行入口与任务列表共用本服务的可见性查询；未回填归属的 WebApp 会话只能匹配同一应用下的终端用户。通用会话接口及 Run 详情仍由 `ConversationService.get_conversation` 检查账号归属，不承担入口分派。

内部 SDK 由 `InternalAgentProtocolService` 自行校验账号、父应用、目标发布应用和会话归属，不依赖 Creator 入口服务。创建 Run、读取会话、读取 Run 和取消 Run 共用本服务内的会话查询；指定 Run 的归属校验通过后才允许失联恢复。内部执行仍使用 `CREATOR_TASK` 来源，并保留历史 WebApp 会话兼容。

## 一次事件交付

```text
LangGraph 原始流
  → Core 规范化：保留原生分片与边界、过滤摘要、转换 JSON 兼容载荷
  → AgentEvent
  → 工作台投影：业务 ID、公开通道与持久化序号
  → AgentProtocolEvent
  → 实时发布与 SSE；并行批量保存，收尾时等待保存完成
```

`AgentExecutionEventBuffer.receive` 对一个 Core 事件最多创建一个公开事件，过滤、载荷适配和缓冲在同一函数内完成。共用的 `create_event` 只分配序号并构造协议外壳。Core 保留框架的正常消息结束和错误事件；前端依据模型错误、子图终态和已提交的业务终态收尾尚未完成的展示记录。

`create_business_event` 与 `create_state_transition` 用于业务主动产生的状态和表单事件。终态批次按“状态 → 表单 → 生命周期”排列，由 Run 服务在同一事务保存。Core 的图结束不代表平台数据已经提交，不能直接作为业务完成通知。

`parse_stored_event` 只解析数据库历史 JSON；实时路径直接构造最终协议对象，不经过历史兼容入口。公开 custom 数据仍按工作台响应 Schema 过滤。

## 历史与重连

`/state` 固定返回会话消息、公开状态、表单、`values.runs` 运行摘要和当前运行的 `metadata`，不接受视图模式，不读取事件表或合成生命周期。摘要中的公开错误取自业务消息，内部异常不对外暴露。停止运行前需要补查 Run ID 时，也读取该响应的 `metadata`。

历史执行详情通过 `/agent-runs/{run_id}/events` 按页读取，当前运行通过 `/stream` 按游标订阅。前端将两条路径的真实事件写入同一个按 Run 管理的集合，按事件 ID 去重；顺序增量直接投影，补入较早历史时只重放对应 Run。运行摘要独立决定已知状态，历史页不能把已结束的运行回退为执行中。

前端由同一个 SSE adapter 向 SDK 和执行过程交付事件，包含 `custom` 上下文事件与子图事件；执行过程不再使用额外的 `useChannel` 订阅或独立滚动缓冲。历史详情在展开时分页加载，上下文面板共享已加载的事件。

SSE 先读取运行状态，再扫描该 Run 的事件，两者都在同一次 Session 清理范围内。观察到终态后仍排空已提交事件，避免查询间的提交让尾批丢失。通知版本只负责唤醒，不存储事件正文。
