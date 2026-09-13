# 工作台协议适配

本目录只负责工作台业务数据与前端协议之间的映射。框架事件解析在 `internal/core/agent_v2/execution/event_normalizer.py`；内部异步子 Agent 的 SDK 接口在相邻的 `internal_agent_protocol` 目录。

## 阅读顺序

1. `agent_workbench_schema.WorkbenchCommandReq.from_mapping`：一次解析命令外壳与参数，兼容 SDK 的嵌套输入及单个／批量回答。`params` 在解析后已经是 `WorkbenchRunInput`、`WorkbenchInputRespondReq` 或 `WorkbenchInputReopenReq`。
2. `command_adapter.py`：消费已解析的参数，分派给启动、回答和重新填写入口，再包装协议响应；不解析原始请求字典。
3. `event_projector.py`：接收 Core 规范化的 `AgentEvent`，绑定业务消息 ID、分配持久化序号并生成 `WorkbenchEvent`。
4. `state_projector.py`：业务消息标识、公开运行状态和表单；不再从快照合成执行事件。
5. `message_projector.py`：业务消息历史转换与活动 Run 消息补齐。

## HTTP 入口

`internal/handler/agent` 按入口组织处理器：`DebugAgentHandler`、`WebAppAgentHandler`、`CreatorTaskAgentHandler` 各负责命令、订阅、状态、模型选项和取消；每个处理器只依赖自己的会话授权入口。`AgentRunHandler` 单独负责 Run 历史事件查询。

Handler 完成各入口鉴权后，按操作需要直接传递账号、应用、会话、创建者和固定调用来源，不构造通用 Context。状态查询只接收会话 ID 和来源，取消与订阅额外校验应用归属；启动和恢复接收完整调用身份。`AgentInvocation` 只在创建 Run 或恢复执行时保存完整输入，JSON 字段保持与历史快照兼容。路由保留原 URL、HTTP 方法和 Flask endpoint，Assistant、OpenAPI 和内部 SDK 的鉴权仍各自独立。

各入口 Service 直接查询并校验允许的会话来源和账号归属，不再将 `allowed_sources` 交给通用会话服务。Creator 的执行入口与任务列表共用本服务的可见性查询；未回填归属的 WebApp 会话只能匹配同一应用下的终端用户。通用会话接口及 Run 详情仍由 `ConversationService.get_conversation` 检查账号归属，不承担入口分派。

内部 SDK 由 `InternalAgentProtocolService` 自行校验账号、父应用、目标发布应用和会话归属，不依赖 Creator 入口服务。创建 Run、读取会话、读取 Run 和取消 Run 共用本服务内的会话查询；指定 Run 的归属校验通过后才允许失联恢复。内部执行仍使用 `CREATOR_TASK` 来源，并保留历史 WebApp 会话兼容。

## 一次事件交付

```text
LangGraph 原始流
  → Core 审核与规范化：消息分片、子图边界、摘要过滤、JSON 兼容载荷
  → AgentEvent
  → 工作台投影：业务 ID、公开通道与持久化序号
  → WorkbenchEvent
  → 批量提交、通知与 SSE
```

`AgentProtocolEventProjector.project` 一次最多返回一个公开事件。这里不导入 LangChain 消息类，不判断供应商 finish_reason，不累积文本或补造消息结束分片。Core 在成功、失败和取消退出时收尾已交付文本。

`generated` 与 `generated_state_transition` 用于业务主动产生的状态和表单事件。终态批次按“状态 → 表单 → 生命周期”排列，由 Run 服务在同一事务保存。Core 的图结束不代表平台数据已经提交，不能直接作为业务完成通知。

`to_public_event` 只解析数据库历史 JSON；实时路径直接构造最终协议对象，不经过历史兼容入口。公开 custom 数据仍按工作台响应 Schema 过滤。

## 历史与重连

`/state` 固定返回会话消息、公开状态、表单、`values.runs` 运行摘要和当前运行的 `metadata`，不接受视图模式，不读取事件表或合成生命周期。摘要中的公开错误取自业务消息，内部异常不对外暴露。停止运行前需要补查 Run ID 时，也读取该响应的 `metadata`。

历史执行详情通过 `/agent-runs/{run_id}/events` 按页读取，当前运行通过 `/stream` 按游标订阅。前端将两条路径的真实事件写入同一个按 Run 管理的集合，按事件 ID 去重；顺序增量直接投影，补入较早历史时只重放对应 Run。运行摘要独立决定已知状态，历史页不能把已结束的运行回退为执行中。

前端由同一个 SSE adapter 向 SDK 和执行过程交付事件，包含 `custom` 上下文事件与子图事件；执行过程不再使用额外的 `useChannel` 订阅或独立滚动缓冲。历史详情在展开时分页加载，上下文面板共享已加载的事件。

SSE 先读取运行状态，再扫描该 Run 的事件，两者都在同一次 Session 清理范围内。观察到终态后仍排空已提交事件，避免查询间的提交让尾批丢失。通知版本只负责唤醒，不存储事件正文。
