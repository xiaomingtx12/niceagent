# Agent 回答测评

测评支持历史回答评分与独立执行验收两种模式。提交时冻结材料、标准和模型引用，模型凭证不写入记录或队列。

## 运行与升级

- HTTP 提交先保存 `queued` 记录，再发送 `internal.task.agent_evaluation_task.run_agent_evaluation` 到现有 Celery 队列；Worker 通过条件更新领取一次任务，完成后写入 `completed` 或 `error`。
- 此次 ORM 调整只有 `AgentEvaluation.finished_at` 改为允许空值，用于排队和运行中记录。已有迁移中的该字段仍为非空，需按项目流程由维护者生成并审核迁移，再执行迁移。本次没有生成、改写或执行迁移。
- 如果旧库仍要求 `finished_at` 非空，提交会返回“测评数据库结构尚未更新”的业务错误，不暴露 SQL 或任务材料。此提示不会修复数据库约束，也不会创建或入队测评；仍需维护者完成迁移。
- 上线时同步更新 HTTP 服务与 Celery Worker，确认 Worker 已注册上述任务。沿用项目现有 Redis/Celery 配置，不引入额外队列系统。
- 历史评分 Celery 软/硬时限为 240/270 秒，执行验收为 720/750 秒，生效依赖 Worker 池支持。超过提交时间 15 分钟的未完成记录会在读取或处理时归为超时，迟到结果不能覆盖它；这是记录状态收敛，并不保证终止外部供应商请求。
- 模型故障不自动重新评分。用户主动新建一次测评才重新消费模型；同一 HTTP 请求重试通过 `request_id` 复用记录。该标识按账号和应用映射为记录主键；复用标识但修改标准、任务或模型会被拒绝。

## 评分口径

`answer-rubric-v3` 必须返回每项的 `score`：完全满足 `1.0/passed`，部分满足 `0.5/failed`，不满足 `0.0/failed`，无法核实 `null/insufficient_evidence`。缺失、档位矛盾、漏项或虚构引用均为评分故障。取消模型自报置信度。

完整得分 `score` 与完整通过率 `pass_rate` 在证据不足时仍为空。新增 `evaluated_score`、`evaluated_pass_rate` 仅基于可评项，`evaluated` 和 `coverage` 显示分母及覆盖率。页面明确区分完整成绩和部分成绩；旧版记录保持原值并标注旧规则。

## 页面

`/evaluations` 直接进入编辑器并选择第一个可用 Agent；顶部下拉切换应用。应用选项按后端上限 50 条分页读取。左栏编辑模型、标准及模板，右栏选择任务并预览输入/回答；历史视图展示记录与报告。任务工作台不显示测评入口。

刷新记录 URL 可恢复后台进度，离开记录会停止前端轮询。模板追加去重并保留自定义标准。网络响应不确定时，原配置再次提交复用同一请求标识；主动新建或调整配置会使用新标识。

## 验证

后端：`.venv/Scripts/python.exe -m pytest tests/test_agent_evaluation.py tests/test_agent_evaluation_runtime.py -q`。

前端：`npx vitest run src/views/space/apps/EvaluationView.test.ts --maxWorkers=2 --minWorkers=1`，以及 `npm run type-check`、`npm run build-only`。

后端测试用内存数据库和模拟模型，实际执行提交、领取、解析与保存；队列发送被替换。前端使用真实 Vue/Arco 组件与受控 API 响应，验证模板、幂等重试、URL 恢复、竞态、分页和部分成绩。本地浏览器预览用固定样本，不调用真实评分模型。

## 代码职责

- `internal/service/agent_evaluation_service.py` 处理授权、快照持久化、幂等提交和任务状态；Worker 仅清理当前记录的超时状态，详情轮询也仅更新指定记录。
- `internal/service/agent_evaluation_grader.py` 负责模型评分、输出解析、证据校验与汇总，不依赖数据库或 Celery。
- 前端 `src/hooks/use-agent-evaluation.ts` 管理请求、提交与轮询生命周期；`EvaluationView.vue` 负责页面组合。切换任务只请求新详情，不刷新列表；迟到的提交结果不会覆盖用户后续导航。
- 测评表的测试镜像保留 ORM 非空约束。另有 PostgreSQL 诊断字段的故障注入测试，区分已知旧库约束、唯一键竞争和其他数据库异常。

## 独立执行并验收

在测评页面选择「执行并验收」，选取历史任务输入、评分模型、标准和最多 1–5 轮评分。使用当前 Agent 草稿的模型、提示词和关键词审核配置；不会把旧答案作为生成输入。模型与提示词在提交时冻结，后续修改草稿不会影响排队任务。

此模式限定为回答与临时文件验收：新建 `StateBackend`、`InMemorySaver` 和独立 `thread_id`，不写原会话或 AgentRun。生产插件/MCP、知识库、Skills、子 Agent、长期记忆及原附件均不加载。工具白名单只允许本次状态内的文件操作和计划更新；禁用 shell 与委派。它不等于完整生产 Agent 的端到端回放，也不执行候选代码测试。

`RubricMiddleware` 使用独立裁判模型。每轮 `inspect_candidate` 无参数读取框架实际状态中的最新答案及文件，支持三类确定性核验：回答包含指定文本、完整 JSON 对象包含顶层字段、虚拟文件存在且非空。候选哈希绑定答案和文件版本；每轮重新取证。最多 48,000 字答案、50 个文件、96,000 字文件总量，超限结束而不截断评分。

原生 `needs_revision` 驱动工作模型修订；`EvidenceRubricMiddleware` 只在其外侧校验 C1…Cn 的完整覆盖、取证调用和确定性结果一致性。空标准结果、跳过取证、程序核验失败却声称 satisfied，都归为 grader_error。0.6.12 的 callback 原始 verdict 与 `_rubric_status` 分开保存，迭代耗尽展示为 max_iterations_reached。只有最终候选与已验收候选的哈希一致且状态 satisfied，应用才标记 accepted。

每轮在官方 callback 之外保存候选、文件、核验结果及差距；持久化失败会停止执行。后续失败保留已保存轮次，accepted 不会被提前写为 true。历史列表只返回状态摘要，完整轮次由详情接口读取；报告展示首轮是否通过、最终状态及总耗时。

运行快照和结果使用现有 `source_snapshot.execution` 与 `result.runtime` JSON 字段，不新增数据库列。原先 `finished_at` 可空迁移仍是异步运行的前提。评分模型需同时支持工具调用及结构化输出；不能用纯文本 JSON 支持推断其兼容性。

`tests/test_agent_evaluation_runtime.py` 使用真实 Deep Agents 图与脚本模型验证两轮修订、预算耗尽、新鲜证据、空标准拒绝、StateBackend 实际文件、命令拒绝和回调外持久化故障。没有使用真实模型凭据或付费请求。
