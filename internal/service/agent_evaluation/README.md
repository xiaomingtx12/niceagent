# Agent 测评阅读入口

平台层面的完整流程、技术难点及分析口径见 [测评、证据与用量分析](../../../docs/platform/implementation/10-测评证据与用量分析.md)；本文件保留代码就近阅读入口。

测评使用提交时冻结的材料和配置。它与生产 Agent 运行相互独立，修改应用后不会改写历史测评。

| 文件 | 职责 |
| --- | --- |
| `agent_evaluation_service.py` | 来源授权、冻结材料、幂等提交、队列投递，选择历史评分或执行验收 |
| `agent_evaluation_store.py` | 原子认领、进度和报告持久化、截止时间与终态保护 |
| `agent_evaluation_grader.py` | 历史回答评分：调用裁判、核对标准及引用、汇总可评项目 |
| `agent_evaluation_runtime.py` | 使用冻结配置装配隔离图，接收 Core 的验收记录并确认最终结果 |

框架中间件统一放在 `internal/core/agent_v2/middleware/`：`evaluation_tool_middleware.py` 限制工具访问，`evidence_rubric_middleware.py` 适配 Rubric 回调。候选提取、哈希和确定性核验放在 `internal/core/agent_v2/execution/evaluation_evidence.py`。Core 不引用测评 Service，进度只通过传入的回调交付。

测评规则、评分分档、标准覆盖和引用约束放在 `internal/entity/agent_evaluation_entity.py`。HTTP Schema 只组装请求与响应，不再承担裁判评分规则。

## 按什么顺序读

先读 Service 的 `submit → process → _evaluate_snapshot`，了解请求如何变成后台任务；再根据测评模式进入 `evaluate_answer` 或 `execute_with_rubric`。数据库状态更新统一看 `EvaluationTaskStore`，不必在评分代码里寻找事务处理。

执行验收内部沿着 `candidate_snapshot → inspect_evidence → EvaluationRound → _execution_result` 阅读：

| 数据 | 含义 |
| --- | --- |
| `EvaluationCandidate` | 当前回答、虚拟文本文件及内容哈希，不包含评分结论 |
| `EvaluationEvidence` | 对该候选执行程序检查得到的结果；与原候选绑定 |
| `EvaluationRound` | 一轮裁判输出，以及平台核验后的状态和对应证据 |
| `result.runtime` | 面向页面的最终报告或进行中进度，仅在输出边界转换成字典 |

`candidate_hash` 比较的是交付内容是否改变，不表示内容正确。`evidence.ok` 只表示程序检查全部通过，不表示语义标准全部满足。没有额外规则时 `ok` 为真，仍然需要裁判读取候选、逐项评分。

## 两种测评

- 历史评分只查看本轮任务和最终回答；缺少附件、工具执行或外部证据时允许弃评。引用必须来自原文，总体评分不能用可评部分替代。
- 执行验收使用冻结的草稿模型、提示词和审核规则，在独立会话和临时文件空间生成与修订，不使用生产工具或长期记忆。

## 后台处理

1. `submit` 保存材料后再投递记录 ID；幂等键冲突时核对原请求，不能更换标准或模型。
2. `process` 通过 `task_store.claim` 收敛过期记录，再以条件更新认领排队任务。重复投递不重复执行。
3. `_evaluate_snapshot` 加载已授权模型后结束读取事务，随后执行历史评分或独立验收。
4. `save_progress` 与 `complete` 在同一条条件更新中检查 `running` 状态和提交后 15 分钟的截止时间，迟到进度和结果不能写入；失败保留已保存轮次。

任务状态为 `queued → running → completed/error`；队列投递失败或排队过期也可直接进入 `error`。这里的 `completed` 表示已生成报告，是否通过需要看历史报告的 `outcome` 或执行报告的 `accepted`。

截止时间在任务认领、进度保存、报告保存和页面查询时检查，不会中断正在阻塞的模型调用。Celery 的软硬超时仍负责限制 Worker 执行时间。

## 候选提取与核验

- 答案只取当前轮最近一次不带工具调用的模型回答；遇到当前提问或尚未完成的工具调用就停止，不回退到旧回答。
- 独立图显式使用 `StateBackend(file_format="v2")`。候选文件只接受该图生成的 UTF-8 文本，格式异常报错，不能跳过文件继续验收；不读取历史 v1 文件或主机文件。
- 回答上限 48,000 字符，文件最多 50 个、合计 96,000 字符。超过上限直接终止，不裁剪证据。
- `contains` 检查原文子串；`file_exists` 检查虚拟文件存在且内容非空白；`json_keys` 检查完整 JSON 对象的顶层字段，同一候选最多解析一次 JSON。
- 内部通过对象属性访问候选和证据；工具返回、进度保存和最终报告通过 `to_dict()` 输出独立容器，避免反复深拷贝整个运行记录。

## Rubric 适配约束

- 当前实现对应 Deep Agents **0.6.12**。`_rubric_status`、`jump_to` 和评分回调格式的适配集中在 Core 的 `evidence_rubric_middleware.py`。
- 同步和异步评分共用 `_begin_round`：冻结当前候选，清空旧证据，再报告进度。
- `_record` 保存本轮记录；标准覆盖、证据读取及通过结论的一致性由 `_validation_error` 检查。
- 原生 Rubric 已负责裁判结论与逐项通过/失败的一致性校验，平台不重写修订循环。
- `EvaluationRound.accepts` 确认末轮状态为通过、最终回答非空、候选及证据哈希与最终状态一致且程序检查通过。
- 进度持久化发生在原生评分回调之外，防止框架吞掉回调异常后继续修订。
- `max_iterations` 限制评分轮数，第一次评分也计入；记录中的 `iteration` 从 0 开始，页面显示时加 1。
