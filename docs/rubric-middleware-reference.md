# RubricMiddleware 实现与版本核对

核对日期：2026-09-10。本文只记录研究结果，未修改运行代码或依赖。

## 结论

`RubricMiddleware` 负责在 Agent 执行过程中验收并驱动修改；历史回答的离线评分可以使用相同的 rubric 和证据工具，但不等于已经接入这个中间件。官方文档明确区分 LangSmith 的离线评分与 RubricMiddleware 的运行时迭代。[1]

项目锁定并实际安装 `deepagents==0.6.12`，同时安装 `langchain==1.3.14`、`langchain-core==1.5.1`、`langgraph==1.2.9`。本地 `deepagents/middleware/rubric.py` 与官方 `deepagents==0.6.12` 标签下源文件在统一换行后完全相同。这个版本已经提供 `RubricMiddleware` 和 `on_evaluation`，不需要为获得回调而升级到教程所用的 0.7.1。[2][3][4]

## 0.6.12 已具备的能力

| 能力 | 实际语义 | 接入时的要求 |
| --- | --- | --- |
| 激活 | 创建 Agent 时安装 middleware，并在调用 state 中传入非空 `rubric`；缺少 rubric 时不评分 | 配置入口和执行输入都要接通 |
| 独立裁判 | 中间件创建独立 `create_agent`，使用 `response_format=GraderResponse` | 裁判模型必须兼容结构化输出；存在证据工具时还需工具调用能力 |
| 证据工具 | `tools` 只提供给裁判；未配置时裁判仅依据 transcript | 对文件、测试、业务状态的判定应提供独立核验工具 |
| 自动修订 | `needs_revision` 生成带 `lc_source="rubric_grader"` 标记的 HumanMessage，并跳回 model | 合成反馈不能当作真实用户消息或直接计入正常用户轮数 |
| 迭代预算 | 默认最多 3 次裁判调用；0.6.12 限制 1–20 次 | 这不是额外允许修改 3 次；第一份答案的验收也计一次 |
| 观测 | 提供 `on_evaluation` 和 `rubric_evaluation_start/end` 事件 | 记录 grading_run_id、从 0 开始的 iteration、逐项结果与 gap |
| 状态 | `_rubric_status` 等字段使用 PrivateStateAttr，不属于公开 I/O schema | 不应假设普通 invoke 返回值包含这些字段；有 checkpointer 时可使用 `get_state(config).values` |
| 恢复 | 相同 rubric 在未终止时延续预算；换 rubric 或已经终止后再调用会开始新的 grading_run_id | 报告应按 grading_run_id 分组，不能混淆不同验收轮次 |

以上由 0.6.12 源码中的 `RubricState`、`__init__`、`before_agent`、`after_agent`、`_ensure_grader` 和 `_compose_update` 核对。[2]

## 终止状态不能都展示为通过

- `satisfied`：裁判认定所有标准通过。
- `needs_revision`：至少一项不通过；预算尚未耗尽时继续修改。
- `max_iterations_reached`：预算耗尽，仍未通过。
- `failed`：rubric 畸形、冲突或无法依据材料完成评估，不是“Agent 执行报错”的同义词。
- `grader_error`：评分模型、凭据、结构化输出等评分过程异常。

后三种终止状态不会改写最后一条 Agent 答案。成功拿到答案或者执行流程正常结束，都不能证明 rubric 已通过。[1][2]

## 必须处理的版本差异

**0.6.12 在最后一次不通过时，回调事件与内部终态不同。** `_finalize_evaluation` 先发送回调和事件，值仍为 `needs_revision`；随后 `_compose_update` 才把 `_rubric_status` 设为 `max_iterations_reached`。0.7.1 则在发送回调前改写 evaluation.result，回调和内部状态均为 `max_iterations_reached`。[2][4]

本次在项目虚拟环境中以模拟裁判运行 `before_agent`/`after_agent`，设置 `max_iterations=1`，实际得到：

```text
callback_result: needs_revision
event_result: needs_revision
internal_status: max_iterations_reached
```

因此保留 0.6.12 时，应读 checkpoint 中的最终状态，或者在适配层根据 `iteration + 1 >= max_iterations` 归一化终态，并保留原始裁判 verdict。不能直接照搬 0.7.1 教程里的末次 callback 判断。

0.7.1 还取消了 20 次硬上限，仅要求正整数；本项目应该沿用自己的成本与时间预算。当前官方 main 又出现 `prepare_messages_for_grader`、`build_grader_state` 等参数，以及覆盖率检查相关字段，这些不属于项目已安装版本的 API，不宜直接引用。[2][4][5]

## 证据与报告的边界

默认 transcript 只保留末尾 30 条消息，必要时补上第一个真实 HumanMessage；单条文本最多 4,000 字符，之后截断。这意味着“工具结果曾经出现在对话里”不保证裁判最终看到了完整证据。[2]

官方基础输出只有 `result`、`explanation` 和逐项 `name/passed/gap`，没有项目现有的 0/50/100 分、证据覆盖率、原文引用或“证据不足”枚举。0.6.12 的 schema 会检查结论与逐项 pass/fail 是否矛盾，但不会校验 rubric 的每一项是否都被返回；空 criteria 的 satisfied 在 schema 层也能通过。若接入中间件，应保留现有业务侧的标准完整性校验和证据校验，不要因使用官方类而移除。[2]

官方示例给裁判提供 `run_test_suite`，用实际测试结果验证代码。这里可复用的是“裁判独立取证”的设计；服务端部署应让证据工具只访问当前账号、当前运行对应的交付物，并在现有隔离执行设施中运行待测代码。工具结果应返回可复核的证据和失败项，不能仅回显 Agent 自己的完成声明。[1]

## 项目接入风险与建议

当前 Agent 装配层使用 `ReviewMiddleware`，没有挂载 `RubricMiddleware`；前者不能代表已实现 rubric 验收。执行器在没有异常、取消或等待输入时默认返回 `completed`，这个值只描述执行生命周期，应额外保留验收结果，不能直接映射成 `satisfied`。参见 `internal/core/agent_v2/assembly/graph/platform_middleware.py`、`internal/core/agent_v2/execution/agent_executor.py`。

公开事件投影器对 `custom` 事件目前只允许 `context_window` 和 `context_compaction` 两类 Schema。仅将 rubric middleware 装入运行链路，其 `rubric_evaluation_start/end` 也不会自动成为有效的前端事件；需要增加明确的事件 DTO、投影和消费逻辑，并保留运行 ID 与轮次信息。参见 `internal/service/agent_protocol/event_projector.py:143`。

建议保留现有“历史快照评分”，另外在独立测评页提供可选的“执行并验收”：建立单独测评运行，把 rubric、裁判模型、证据工具和预算传入执行链路，存储每轮 verdict、反馈、证据及终态。自动修改会改变被测答案，因此应保留初始结果与修订后结果，让用户能够区分原始表现和验收后表现。该模式可以全部放在测评页，任务工作台不增加测评控件，符合当前界面要求。

## 来源

1. [官方 Grading rubrics 文档](https://docs.langchain.com/oss/python/deepagents/rubric)，核对运行时迭代、状态、工具与示例；Markdown 原文为 `https://docs.langchain.com/oss/python/deepagents/rubric.md`。
2. [官方 deepagents==0.6.12 rubric.py](https://github.com/langchain-ai/deepagents/blob/deepagents%3D%3D0.6.12/libs/deepagents/deepagents/middleware/rubric.py)，与本地 `.venv/Lib/site-packages/deepagents/middleware/rubric.py` 一致。
3. 项目 `requirements.txt:25` 及项目虚拟环境的 `importlib.metadata.version()` 实测结果。
4. [官方 deepagents==0.7.1 rubric.py](https://github.com/langchain-ai/deepagents/blob/deepagents%3D%3D0.7.1/libs/deepagents/deepagents/middleware/rubric.py)，核对回调终态与参数差异。
5. [官方 main rubric.py](https://github.com/langchain-ai/deepagents/blob/main/libs/deepagents/deepagents/middleware/rubric.py)，仅用于提醒当前文档与已安装版本的差异；main 会随上游提交变动。
6. [用户提供的 Datawhale 第 13 章](https://datawhalechina.github.io/deepagents-in-action/chapters/ch13-grading-rubrics/)，用于定位学习背景及上述官方参考源。
