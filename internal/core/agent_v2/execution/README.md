# Agent V2 执行层阅读入口

本包是 Core 与 Deep Agents 图之间的执行边界：驱动一次 v3 图运行，把框架事件流收敛为平台稳定契约（`AgentEvent` / `AgentExecutionResult`），并管理执行器的资源生命周期。上游由 `AgentFactory` 装配创建，下游由 `AgentExecutionService` 的后台线程调用。

`AgentExecutor.execute` 按顺序完成输入校验、图执行、资源关闭和结果汇总。`_run_graph` 独占框架流和回调，提前取消直接返回；运行时取消继续收集根状态，但停止公开事件。框架异常转成返回的执行错误，输入错误保持上抛，两种路径都会关闭运行资源。

## 一次执行的事件链路

```text
graph.stream_events(version="v3")            框架原始流（GraphRunStream，调用方驱动泵）
  → _run_graph 内的消费循环                     收集根 values 状态 + 处理取消排空
  → AgentEventNormalizer.accept              过滤摘要、适配完整消息、序列化为 AgentEvent
  → on_event 回调                            Service 投影为公开事件，再交给 Buffer 发布与保存
执行结束
  → execute 返回结果                         状态、回答、用量收敛为 AgentExecutionResult
  → _finalize_run（Run 服务）                 终态、快照与终态事件同一事务落库
  → 前端 event-projection                    依据真实终态收尾尚未结束的展示记录
```

框架事件信封即 langchain-protocol 线协议（`method` / `params.namespace` / `params.data` / `seq`），与前端 `@langchain/protocol` 同源；平台侧的业务身份绑定（run 维度序号、business_message_id）发生在下游 `internal/service/agent_protocol`，不在本包。

这里采用官方 v3 raw event 入口，保留主图、子图、工具和消息之间的到达顺序供落库与重放。消息载荷按框架 `MessagesTransformer` 的规则处理：协议分片直接保留，完整 `BaseMessage` 通过框架 `message_to_events` 转换，`AIMessageChunk` 和 `ToolMessage` 按当前 SDK 的 transformer 规则跳过，避免重复回放。参考 [Deep Agents Event Streaming](https://docs.langchain.com/oss/python/deepagents/event-streaming#consume-concurrently)。

`AgentEvent.model_run_id` 来自消息 metadata 的框架 `run_id`，与平台 Run ID 不同。Service 将其写入 `params.metadata.my_dify.model_run_id`，前端按 namespace 和模型调用 ID 关联分片；历史事件没有该字段时沿用节点路由。Core 不缓存文本、不从工具或图终态补造 `message-finish`；前端收到原生消息错误或运行/子图终态后，保留已显示正文并标记失败、取消或中断。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `agent_executor.py` | 单次执行的编排入口：v3 流驱动、协作取消（RunControl 排空）、资源状态机、事件交付与清理兜底 |
| `event_normalizer.py` | 框架事件适配：公开通道与摘要过滤、完整消息转换、模型调用身份及 JSON 兼容载荷；消息字段在 AgentEvent 创建时解析 |
| `message_text.py` | 正文提取：`extract_text` 拼接文本块，`extract_final_answer` 选取本轮最后一条有正文的 AI 回答 |
| `json_serialization.py` | 出口序列化：`to_json_value` 将框架消息、UUID 和嵌套数据转换为 JSON 兼容值 |
| `runtime_resources.py` | 一次执行的资源打包：Backend、Checkpointer、Store、Skill 投影与运行上下文 |
| `model_progress.py` | 在首个 Token 前发布模型请求边界，与消息流共用调用 ID；SDK 标记的摘要调用仍单独发布压缩阶段事件 |
| `user_input_interrupt_tracker.py` | 识别 `ask_user` 工具的 `GraphInterrupt`，让事件交付把正常暂停与真实工具失败区分开 |
| `evaluation_evidence.py` | 测评取证：从实际状态提取候选答案与文件，做确定性核验，不调用评分模型 |
| `workspace_delivery.py` | 显式交付（deliver_file）路径：把本轮工作副本落回持久化来源 |

## 事件类型从哪里找

`entity/stream_event.py` 集中定义通道、消息边界、增量格式与模型进度阶段。枚举值保留现有协议字符串，业务判断使用具名成员。

`entity/message_event.py` 是消息载荷的唯一解释入口：`MessageEvent` 提供消息类别、内容块索引和已解析增量，`TextDelta` 表达文本追加，`FieldDelta` 表达结构覆盖。content 仅用于开始和结束，增量只读取 delta。实时过滤与历史合并共用这套规则，旧 content 增量和缺失起点不再兼容。

`AgentEvent.data` 保留可转发的原载荷；messages 事件额外携带已解析的 `message`。工具、图状态和自定义进度保留各自字段，不强行套用消息结构。

## 修改时的边界

- 本包只解释框架流，不写数据库、不知道 Run/Message 模型；平台身份与持久化全部在 `service/agent` 一侧。
- `AgentEventNormalizer` 每次执行独占一个实例，交付序号不可跨执行复用。
- 正文提取复用 `message_text`，出口序列化复用 `json_serialization.to_json_value`；模型响应拆包只供 `ContextWindowMiddleware` 内部读取用量。
- v3 流式协议仍是 beta：框架升级若改变消息载荷形态（例如 chunk 重新出现），先查 `event_normalizer` 的消息分支与 `langgraph.pregel._messages` 的 handler 选择逻辑，不要直接加兜底代码。
- 框架消息的摘要调用带 `lc_source=summarization` 元数据，`_normalize_messages` 依赖它过滤内部压缩正文，删除该过滤会让摘要泄漏为用户回答。

## 模型输入与公开消息

`model_input.py` 在新输入副本中加入一次本轮规划模式，原始 ID、用户正文和媒体保留；不将动态选项追加到 system。AgentExecutor 的恢复分支只使用 Command(resume)，不会再插入该输入。模型循环与压缩使用已有消息历史。

内部 `platform_input_options` 标记记录选项与原正文形态。完整消息事件转换和 Service 的图状态投影共用 public_input_message，只有标记和首块一致时才移除内部块，不解析或删除用户正文中的同名标签。公开气泡显示原文，模型 checkpoint 保留内部任务选项。
