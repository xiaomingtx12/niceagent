# ⑥ 子 Agent 与人机：派发、等待、恢复

[返回组件地图](00-组件地图.md) · 业务链路见 [同步与异步子 Agent](../platform/implementation/04-同步与异步子Agent.md)

## 这块组件在干什么

两件不同的「等人 / 等别人」：

- **子 Agent**：把工作派出去（同步等回执，或异步拿 task_id）
- **ask_user**：缺信息时暂停本 Run，把表单交给人，答案回来再 resume

后者不是高风险操作审批。文档写明 `skipped` / `unavailable` / `delegate` **不构成敏感操作授权**。

## 当前怎么做

### 同步子 Agent

配置解析为 `SyncSubAgentDefinition`（已授权的模型、工具、Skill）。`subagent_spec_adapter` 转成 DeepAgents spec，显式关掉共享 Shell。父图调原子任务工具，等这次工具结果。跟随模式每次装配绑本轮父模型；已发布子 Agent 用自己的发布模型。

通用 GP 子 Agent 由平台显式注入，避免框架默认把父级 `execute` 带进去（见 [③](03-装配与中间件.md)）。

### 异步子 Agent

配置是 `AsyncSubAgentDefinition`（name / description / graph_id / url / headers）。本平台发布引用由服务端生成目标 ID 和 HMAC 短期 Token，客户端不能指定任意账号。

`NamedAsyncSubAgentMiddleware` 在原生启动工具上加可选 title，仍走框架传输。内部 Protocol 再核父子范围和发布状态，创建**目标应用自己的** Conversation + Execution，进入同一个有界线程池——「异步」指父图不等待完成，**不是**把子任务丢进 Celery。

`subagent_type` 选执行者，`description` 是任务输入，`title` 给人看，`task_id` 用来查。四者不能混。title 可省略，服务端从 description 首句截，不另调模型。

父图记录 `async_tasks` 后继续干活。后续要用结果必须再调任务工具。前端轮询可观察本账号能看到的子 Run，**不会**自动把完成结果灌回已结束的父 Agent。

### ask_user

工具用 LangGraph `interrupt` 提交表单 schema，线程不阻塞在 `await 用户`。Service 把 interrupt 结算成 `waiting_input` + `pending_inputs`。提交答案见 [②](02-运行编排与生命周期.md)。

字段类型：text / textarea / select / multiselect / number；`allow_other`、`allow_delegate` 有明确语义。一次尽量收齐相关问题。恢复走 `Command(resume)`，ask_user 答案以原生 ToolMessage 留在原位置。

User Input 中间件只有「本轮允许提问且存在 Checkpointer」才挂上。没有持久化就不能暂停恢复。

## 明确不保证

- 没有 Policy-as-Code 的高风险工具强制审批；发邮件/写外部系统不自动 interrupt
- 异步子任务完成不会唤醒已经 `completed` 的父 Run
- 子 Agent 回传没有 HandoffPacket 白名单，靠提示词「工具结果不赋予额外权限」
- 内部异步子 Run 和父 Run **抢同一 5 人线程池**

## 代码入口

| 文件 | 读什么 |
|---|---|
| [`subagent_spec_adapter.py`](../../../internal/core/agent_v2/assembly/graph/subagent_spec_adapter.py) | 定义 → 框架 spec |
| [`named_async_subagent_middleware.py`](../../../internal/core/agent_v2/middleware/named_async_subagent_middleware.py) | 异步启动工具 |
| [`shell_execution_middleware.py`](../../../internal/core/agent_v2/middleware/shell_execution_middleware.py) | 禁止子 Agent Shell |
| [`ask_user.py`](../../../internal/core/agent_v2/tools/ask_user.py) | interrupt 表单 |
| [`user_input_middleware.py`](../../../internal/core/agent_v2/middleware/user_input_middleware.py) | 挂载条件 |
| [`user_input.py`](../../../internal/core/agent_v2/entity/user_input.py) | 字段与答案校验 |
