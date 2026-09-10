# Agent 可观测性

从 `PlatformAgentRunObserver` 开始阅读。每个 Run 创建一个实例，作为框架 Callback 和 `AgentTelemetry` 的输出端；业务调用方不需要单独装配内部模块。

| 文件 | 职责 |
| --- | --- |
| `run_observer.py` | 调用开始/结束、父子关联、计数、Run 终态与快照 |
| `callback_metadata.py` | 从模型和工具回调中提取有界标识，不保留输入输出正文 |
| `token_usage.py` | 将不同 Provider 的用量字段归一化 |
| `summary_progress.py` | 发布摘要生成进度，维护子图 writer；不参与用量统计 |
| `telemetry.py` | 接收 Workspace/Sandbox 事件，附加运行作用域，隔离输出失败 |

数据流：框架回调 → Observer → `build_run_usage()` / `snapshot()` → Message 统计字段 / AgentRun.observability。

Workspace/Sandbox 通过 `AgentTelemetry(observer.record, attributes=...)` 记录诊断事件。未配置记录函数时立即返回，不构造事件对象；无需额外的输出协议或空实现类。

`usage` 是用量汇总来源；`summary` 是兼容已有快照格式的概览投影，Token 字段从 `usage` 生成，不另外累计。`spans` 保存单次调用明细，`stages` 保存平台阶段耗时，`events` 保存 Workspace/Sandbox 诊断事件。后三者不是默认展示指标。

每个 Run 最多保存 500 条 Span、100 条阶段耗时、200 条事件；超出部分计入 `dropped`。调用数与 Token 汇总独立于明细上限，不能从截断后的 Span 列表重新计算。

计数口径：

- 模型调用数包含摘要及同步子 Agent 的真实模型调用；工具调用数包含子 Agent 调度，子 Agent 次数是其中的子集，不能再相加。
- 重试次数仅来自 `on_retry` 回调。框架未上报的内部重试不可见；同名调用再次出现不能作为重试证据。新快照采用此口径，历史快照中的推断重试数不会重算。
- 调用错误数不是 Run 失败数：工具出错后 Agent 仍可能完成任务。
- 缺失用量与真实零用量由 `usage_present` 区分。缓存/推理 Token 是明细，不额外累加到总 Token。

统计页由 `AnalysisService` 从 Message 计算，只覆盖过去 7 个完整自然日内**有回复的消息**，不能据此推导运行成功率。默认显示回复次数、活跃用户、平均回复耗时、总 Token；其余用量和调用明细按需展开。平均耗时包含工具及等待时间，输出吞吐为总输出 Token / 总回复耗时，费用只反映已记录的计价结果。

修改后的离线回归：`python -m pytest tests/test_observability.py tests/test_context_compaction.py -q`。
