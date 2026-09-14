# Agent V2 执行层阅读入口

本包是 Core 与 Deep Agents 图之间的执行边界：驱动一次 v3 图运行，把框架事件流收敛为平台稳定契约（`AgentEvent` / `AgentExecutionResult`），并管理执行器的资源生命周期。上游由 `AgentFactory` 装配创建，下游由 `AgentRunService` 的后台线程调用。

## 一次执行的事件链路

```text
graph.stream_events(version="v3")            框架原始流（GraphRunStream，调用方驱动泵）
  → _consume_stream                          收集根 values 状态 + 处理取消排空
  → AgentEventDelivery.accept                输出审核（启用审核时整轮暂存，成功后回放）
  → AgentEventNormalizer.accept              框架事件 → AgentEvent（内容块生命周期、JSON 载荷、单执行序号）
  → on_event 回调                            交给 AgentRunEventBuffer 批量落库并通知订阅
执行结束
  → delivery.finish()                        补齐取消/异常截断的消息结束分片
  → _build_result                            状态、回答、用量收敛为 AgentExecutionResult
  → _finalize_run（Run 服务）                 终态、快照与终态事件同一事务落库
```

框架事件信封即 langchain-protocol 线协议（`method` / `params.namespace` / `params.data` / `seq`），与前端 `@langchain/protocol` 同源；平台侧的业务身份绑定（run 维度序号、business_message_id）发生在下游 `internal/service/agent_protocol`，不在本包。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `agent_executor.py` | 单次执行的编排入口：v3 流驱动、协作取消（RunControl 排空）、资源状态机、结果收敛与清理兜底 |
| `event_delivery.py` | 输出审核与事件交付：启用审核时暂存整轮原始事件，成功后按序回放；失败/取消即释放缓存 |
| `event_normalizer.py` | 框架事件规范化：消息内容块状态机、工具/生命周期边界收尾、分支 namespace 解析，出口只交付 JSON 兼容的 `AgentEvent` |
| `message_parser.py` | 框架对象解析：模型响应包装拆包（`response_messages`）、content-block 文本提取、JSON 值序列化（`json_value`）、本轮 AI 正文扫描 |
| `runtime_resources.py` | 一次执行的资源打包：Backend、Checkpointer、Store、Skill 投影与运行上下文 |
| `summary_progress.py` | 摘要生成进度观测：只发布 SDK 标记（`lc_source=summarization`）的压缩阶段事件 |
| `user_input_interrupt_tracker.py` | 识别 `ask_user` 工具的 `GraphInterrupt`，让事件交付把正常暂停与真实工具失败区分开 |
| `evaluation_evidence.py` | 测评取证：从实际状态提取候选答案与文件，做确定性核验，不调用评分模型 |
| `workspace_delivery.py` | 显式交付（deliver_file）路径：把本轮工作副本落回持久化来源 |

## 修改时的边界

- 本包只解释框架流，不写数据库、不知道 Run/Message 模型；平台身份与持久化全部在 `service/agent` 一侧。
- `AgentEventNormalizer` 每次执行独占一个实例（文本状态机不可跨执行复用）；`json_value` 是唯一允许被外部直接复用的工具函数。
- v3 流式协议仍是 beta：框架升级若改变消息载荷形态（例如 chunk 重新出现），先查 `event_normalizer` 的消息分支与 `langgraph.pregel._messages` 的 handler 选择逻辑，不要直接加兜底代码。
- 审核（`ReviewMiddleware`）同时作用于 `wrap_model_call` 响应和事件交付两条路径；未通过审核的事件缓存会被整体丢弃，不会进入 normalizer。
- 框架消息的摘要调用带 `lc_source=summarization` 元数据，`_normalize_messages` 依赖它过滤内部压缩正文，删除该过滤会让摘要泄漏为用户回答。
