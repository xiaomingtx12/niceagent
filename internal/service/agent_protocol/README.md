# Agent Protocol 适配阅读入口

本目录连接 Core 事件与工作台协议。投影函数不查询数据库；内部 SDK 服务通过工作台服务发起业务操作。

| 文件 | 职责 |
| --- | --- |
| `command_adapter.py` | 分派 SDK 命令到独立业务入口，并把业务结果转换为 SDK 响应 |
| `event_projector.py` | 原始事件顺序校验、通道分派、文本流累计、公开事件校验及事件编号 |
| `message_projector.py` | 业务消息转换，以及活动 Run 快照与历史消息合并 |
| `state_projector.py` | 执行结果的公开状态与生命周期、待填任务和历史运行状态投影 |
| `internal_agent_protocol_service.py` | 将内部 SDK 的会话、运行和取消请求转交工作台服务 |
| `internal_auth.py` | 内部调用的短期签名凭证、授权范围解析，以及异步调用 URL／请求头构造 |

## 事件转换顺序

HTTP Handler 先取得可信调用上下文，再交给 `execute_command`。`run.start`、`input.respond`、`input.reopen` 分别调用启动、提交答案和恢复表单，业务服务不接收命令 ID。内部发布 Agent 直接调用业务启动入口，不先包装成 SDK 成功响应再拆开。

`AgentProtocolEventProjector.project_many` 先消费并校验源序号，再分派到消息或状态处理。
一个原始事件可以生成多个公开事件；公开序号与源序号分别维护。

`project_many`、`finish_pending_messages` 和 `generated_state_transition` 直接返回 `list[WorkbenchEvent]`；`generated` 返回单个 `WorkbenchEvent`，用于工作台生成状态、表单和生命周期事件。序号分配、公开通道过滤和 Schema 校验均在投影器内完成，调用方不再处理事件字典或重复校验。

`to_public_event` 保留为原始字典的转换入口，外部仅在读取数据库历史载荷时使用。已投影的实时事件直接交给缓冲和持久化；内部被过滤的事件仍可能占用公开序号，重连游标不要求连续。

- 消息分片按图路径和节点隔离，由 `_TextStream` 保存当前累积正文。
- 工具开始只封存当前图的文本；子图结束封存对应子树，不截断并行的其他子图。
- 根图结束不直接公开最终状态，最终生命周期事件由执行服务落库后通知。
- 框架摘要和原始 `input.requested` 不直接进入公开流。

`generated_state_transition` 统一生成“状态 → 待填表单 → 生命周期”事件批次。
调用方负责在同一事务中保存整批事件和业务状态，再通知订阅者。

## 执行结果投影

`project_execution_state` 接收 Core 结果和已读取的历史快照，只公开 todos、files、async_tasks 和经过校验的表单，沿用已提交的 input_history。`project_execution_lifecycle` 接收结束状态、消息标识和已脱敏的公开错误，生成完成、失败、取消或等待填写的展示字段。

投影函数不查询数据库、不判断业务异常、不修改消息记录。执行服务负责读取快照、确定业务消息状态与公开错误，再把投影结果交给 `finalize_run` 原子提交。后台启动失败复用生命周期投影，但保留原有事件外壳；历史水合仍使用独立入口和已提交序号。

## 历史恢复

`project_conversation_messages` 从业务消息恢复对话；`merge_running_messages` 只补齐当前活动 Run 的消息。
`project_run_history` 用数据库快照和终态补齐事件历史。合成恢复事件复用已提交序号，不推进 SSE 游标。
