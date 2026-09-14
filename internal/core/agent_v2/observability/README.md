# Agent 用量统计

从 `PlatformAgentRunObserver` 开始阅读。每次执行独占一个回调实例，累计模型、工具调用数和 Provider 返回的 Token 用量。暂停恢复沿用业务 `trace_id`，新的执行租约由业务 Run 管理。

| 文件 | 职责 |
| --- | --- |
| `run_observer.py` | 活动调用登记、用量累计、重复结束保护和统计冻结 |
| `callback_metadata.py` | 请求/响应模型名称与摘要进度的异常分类 |
| `token_usage.py` | 将不同 Provider 的用量字段归一化 |
| `internal/entity/token_usage.py` | 框架和业务模型共用的片段用量、模型累计与历史明细别名规则 |

数据流：框架回调 → `AgentExecutionResult.usage` → `finalize_run()` 在 Run 锁内累计到 Message。`AgentRun.executor_id` 校验执行权，`last_settled_executor_id` 防止同一片段重复结算；消息统计、结算标识、状态和结束事件在同一事务提交。任一步骤失败全部回滚。

固定统计直接保存在 Message 的 Token 与调用次数列；只有按模型的动态明细使用 `Message.token_usage` JSON。Run 不再保存 `observability`；执行结果也不再重复携带观测快照。用量移除无消费者的版本、重复标识、模型名称列表、模型数量、来源与调用覆盖数。`usage_present` 仅在执行结果中区分实测零用量与未返回用量。

统计口径：

- 模型调用数包含摘要及同步子 Agent 的真实模型调用；工具调用数包含子 Agent 调度。
- 模型失败、取消或未返回 Token 时仍保留调用次数，不虚构 Token 消耗。
- 按模型保存输入、输出、总 Token 及输入/输出明细；实际模型名优先于请求的配置别名。
- 缓存/推理 Token 是明细，不额外累加到总 Token；别名在每次聚合前归一化。
- `finish()` 冻结统计并释放活动调用，拒绝迟到回调；结束回调的消费和累计在同一把锁内，不能重复计费。

页面“执行过程”使用 AgentRunEvent。摘要进度由 `execution/summary_progress.py` 的独立回调发布。装配、沙箱和首字交付的耗时继续使用日志。

统计页由 AnalysisService 从 Message 计算，只覆盖过去 7 个完整自然日内有回复的消息。平均回复耗时包含工具等待及尾批交付，输出吞吐为总输出 Token / 总回复耗时，费用只反映已记录的计价结果。

数据库迁移由维护者按仓库约定生成并执行。本次结构变更为新增 `last_settled_executor_id` 并删除 `observability`；迁移已有数据时，应在删列前将旧 JSON 的 `execution_id` 转存到结算列。已累计用量沿用 Message 中的统计和模型明细，不能把旧 Run 汇总再累加一次。代码不自动修改数据库或回填历史数据。

验证重点：暂停恢复后用量与模型明细累计、重复结算与旧执行实例拒绝、事务回滚后可重试、缺失用量不清空历史统计、历史模型明细别名兼容、公开执行事件与微信用量保存。按项目约定不新增测试文件。
