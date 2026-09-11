# Agent Protocol 适配阅读入口

本目录连接 Core 事件与工作台协议。投影函数不查询数据库；内部 SDK 服务通过工作台服务发起业务操作。

| 文件 | 职责 |
| --- | --- |
| `command_adapter.py` | 分派 SDK 命令到独立业务入口，并把业务结果转换为 SDK 响应 |
| `event_projector.py` | 原始事件顺序校验、通道分派、文本流累计、公开事件校验及事件编号 |
| `message_projector.py` | 业务消息转换，以及活动 Run 快照与历史消息合并 |
| `state_projector.py` | 公开快照、待填任务和历史运行状态投影 |
| `internal_agent_protocol_service.py` | 将内部 SDK 的会话、运行和取消请求转交工作台服务 |
| `internal_auth.py` | 内部调用的短期签名凭证、授权范围解析，以及异步调用 URL／请求头构造 |

## 事件转换顺序

HTTP Handler 先取得可信调用上下文，再交给 `execute_command`。`run.start`、`input.respond`、`input.reopen` 分别调用启动、提交答案和恢复表单，业务服务不接收命令 ID。内部发布 Agent 直接调用业务启动入口，不先包装成 SDK 成功响应再拆开。

`AgentProtocolEventProjector.project_many` 先消费并校验源序号，再分派到消息或状态处理。
一个原始事件可以生成多个公开事件；公开序号与源序号分别维护。

- 消息分片按图路径和节点隔离，由 `_TextStream` 保存当前累积正文。
- 工具开始只封存当前图的文本；子图结束封存对应子树，不截断并行的其他子图。
- 根图结束不直接公开最终状态，最终生命周期事件由执行服务落库后通知。
- 框架摘要和原始 `input.requested` 不直接进入公开流。

`generated_state_transition` 统一生成“状态 → 待填表单 → 生命周期”事件批次。
调用方负责在同一事务中保存整批事件和业务状态，再通知订阅者。

## 历史恢复

`project_conversation_messages` 从业务消息恢复对话；`merge_running_messages` 只补齐当前活动 Run 的消息。
`project_run_history` 用数据库快照和终态补齐事件历史。合成恢复事件复用已提交序号，不推进 SSE 游标。
