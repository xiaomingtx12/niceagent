# Agent 可观测性

从 `PlatformAgentRunObserver` 开始阅读。每次执行创建一个实例，作为框架 Callback 和 `AgentTelemetry` 的输出端。暂停恢复沿用业务 `trace_id`，使用新的 `execution_id`；持久化时合并到同一个 Run。

| 文件 | 职责 |
| --- | --- |
| `run_observer.py` | 调用开始/结束、父子关联、计数、单次执行终态与快照 |
| `callback_metadata.py` | 从模型和工具回调中提取有界标识，不保留输入输出正文 |
| `token_usage.py` | 将不同 Provider 的用量字段归一化 |
| `run_snapshot.py` | 合并执行片段、累计用量并限制整个 Run 的明细容量 |
| `telemetry.py` | 接收 Workspace/Sandbox 事件，附加运行作用域，隔离输出失败 |

数据流：框架回调 → Observer → 单次执行快照 → `finalize_run()` 在 Run 锁内合并快照 → 同一份汇总投影为 Message 统计字段和 AgentRun.observability。执行租约防止旧实例写入，`execution_id` 防止同一片段重复累计。

摘要生成通知由 `execution/summary_progress.py` 中的独立 Callback 发布；每次调用单独串行发布，避免持有全部摘要调用共用的状态锁执行 writer。正常提问暂停由 `execution/user_input_interrupt_tracker.py` 识别，两者都由执行器管理生命周期。

Workspace/Sandbox 通过 `AgentTelemetry(observer.record, attributes=...)` 记录诊断事件。未配置记录函数时立即返回，不构造事件对象；无需额外的输出协议或空实现类。

属性规范化集中在 `entity/telemetry.py`，只保证有限数值、标量过滤和字符串截断。模型/工具元数据按白名单提取，主动埋点仍须由调用方排除敏感内容；事件不能覆盖已绑定的运行作用域属性。

`usage` 是用量汇总来源；`summary` 是兼容已有快照格式的概览投影，Token 字段从 `usage` 生成，不另外累计。`spans` 保存单次调用明细，`stages` 保存平台阶段耗时，`events` 保存 Workspace/Sandbox 诊断事件。后三者不是默认展示指标。

每个 Run 最多保存 500 条 Span、100 条阶段耗时、200 条事件、100 条执行片段概览；超出部分计入 `dropped`。`execution_count`、调用数与 Token 汇总独立于明细上限，不能从截断后的列表重新计算。

`duration_ms` 是各执行片段的耗时之和，不包含两次执行之间等待用户填写表单的时间。Message.latency 累计各次回复处理耗时，包含工具等待及尾批交付；它与首尾墙钟时间差、模型生成耗时都不同。

计数口径：

- 模型调用数包含摘要及同步子 Agent 的真实模型调用；工具调用数包含子 Agent 调度，子 Agent 次数是其中的子集，不能再相加。
- 重试次数仅来自 `on_retry` 回调。框架未上报的内部重试不可见；同名调用再次出现不能作为重试证据。新快照采用此口径，历史快照中的推断重试数不会重算。
- 调用错误数不是 Run 失败数：工具出错后 Agent 仍可能完成任务。
- `model_count` 来自实际观察到的模型身份，与用量是否返回无关。结束回调有实际模型名时替换本次调用的配置别名。
- `usage_present` 表示至少一次调用提供用量；`usage_call_count` / `missing_usage_call_count` 表示已提供和未提供用量的调用数。执行期间未结束的模型也属于尚未提供用量。缓存/推理 Token 是明细，不额外累加到总 Token。
- Token 明细在每次调用聚合前统一别名；顶层缓存字段归入输入，顶层推理字段归入输出。历史字典也使用 `entity/token_usage.py` 的同一套别名规则读取。
- `finish()` 后拒绝新的调用、重试和诊断记录，仅增加 `dropped.late_records`。缺少结束回调的调用标为 `incomplete`，取消时标为 `cancelled`，不根据 Run 成功或失败推断调用成功或报错。

统计页由 `AnalysisService` 从 Message 计算，只覆盖过去 7 个完整自然日内**有回复的消息**，不能据此推导运行成功率。默认显示回复次数、活跃用户、平均回复耗时、总 Token；其余用量和调用明细按需展开。平均耗时包含工具及等待时间，输出吞吐为总输出 Token / 总回复耗时，费用只反映已记录的计价结果。

快照版本为 2，兼容合并已有版本 1 的已保存用量。旧数据缺少的调用覆盖数保留为 `null`；此前恢复执行已覆盖的历史明细无法从剩余快照还原，不自动回填历史数据库。

验证重点：暂停恢复前后的总量与按模型统计一致、重复保存不累计、缓存别名去重、缺失与零用量区分、明细截断不影响总量、结束后的迟到回调被忽略、摘要通知保持开始/结束顺序。按项目约定不新增测试文件。
