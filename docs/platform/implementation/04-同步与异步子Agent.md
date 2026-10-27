# 同步与异步子 Agent

[返回模块目录](README.md)

## 执行方式的差别

| 对比项 | 同步子 Agent | 异步子 Agent |
| --- | --- | --- |
| 父 Agent 调用 | 原生子任务工具，等待这次工具结果 | `start_async_task` 接受成功后返回任务标识 |
| 结果使用 | 子任务结束后进入父工具回执 | 后续通过任务工具查询状态与结果 |
| 本平台发布引用 | 装配发布 Agent 为同步子图规格 | 通过内部 Protocol 创建目标应用的会话和 Run |
| 父任务可否继续 | 通常需要等本次工具返回；并行调度是另一维度 | 派发完成后可以继续其他工作 |
| 跨服务能力 | 取决于具体工具/子图 | 可配置外部 Protocol 目标 |

这里的“异步”描述父子任务之间不阻塞等待，不意味着本平台已经把子任务迁到了 Celery。

## 一次内部异步派发

1. 应用配置解析为 `AsyncSubAgentDefinition`，包含执行者名称、说明、目标 graph ID、URL 和 Header。
2. 本平台发布引用由服务端生成目标 ID 和内部签名凭证，客户端不能指定任意账号或伪造 Token Header。
3. `NamedAsyncSubAgentMiddleware` 在原生启动工具上增加可选 title，仍调用框架原生传输。
4. 内部 Protocol 校验凭证、应用状态、父子范围和会话归属，创建目标应用 Run。
5. 目标 Run 进入 `AgentExecutionService` 的有界线程池；父图记录 async_tasks 并继续。
6. 后续查询工具取得真实任务状态；前端也可以独立查询当前账号可访问的本平台子 Run。

外部目标的部署方式由其服务决定，不能据本平台实现推断外部目标也使用线程池。

## 名称、任务说明和任务标识

`subagent_type` 用于选执行者；`description` 是完整任务输入；`title` 是便于用户辨认的任务名称；`task_id` 用于精确查询。四者不能互换。

title 可省略，服务端从 description 首句提取有限长度标题，不额外调用模型。成功的 Command 更新同时保存名称和说明；启动失败不伪造任务记录。async_tasks reducer 按任务 ID 合并，状态刷新时保留原名称和初始说明，避免并行更新互相覆盖。

## 前端如何观察

工作区把文件、后台任务、启用的记忆放入同一入口。后台任务以名称和派发时间识别，ID 保留为技术信息；数量放在面板中，顶部入口不使用累计数字暗示待办数。

`AgentWorkspace.vue` 对可访问的本平台未结束任务串行分批查询，每批最多 50 个，后续间隔约 3 秒；页面隐藏时暂停。列表每次展示 30 项，详情按“执行过程 → 结果”展示，过程展开后才加载快照。

这套轮询更新的是页面观察结果，**不会自动把完成结果注入父图，也不会自动唤醒已经结束的父 Agent**。要让父 Agent 汇总，仍需任务工具调用或后续对话驱动；全站完成通知与自动续跑是另一项业务能力。

## 关键难点与故障定位

- 派发接受、首段输出、任务完成是三个阶段；“已启动”不能作为成功交付证明。
- 如果启动接口返回 500 且父图没有 task_id，要查目标协议服务和 Worker 日志，不能直接断言“模型参数一定正确”。
- 有 task_id 但没有首段输出时，核对目标 Run 是否认领、装配是否结束、模型是否发起请求、是否在运行工具。
- 内部 Token 有有效期。长任务之后的再次查询还需要关注凭证有效性，不能把所有查询失败显示为继续运行。
- 父图记录和数据库子 Run 状态可能具有时间差；真实终态不应被较旧的启动快照覆盖。

## 阅读入口

| 文件 | 关注点 |
| --- | --- |
| [Factory](../../../internal/service/agent/agent_factory.py) | `_prepare_async_subagent` 与发布引用 |
| [命名适配](../../../internal/core/agent_v2/middleware/named_async_subagent_middleware.py) | 可选 title、成功回执、并行 reducer |
| [内部授权](../../../internal/service/internal_agent_protocol/auth.py) | HMAC、范围、有效期 |
| [内部协议服务](../../../internal/service/internal_agent_protocol/service.py) | create/get/cancel 与真实 Run 的映射 |
| [工作区](../../../../my-dify-ui/src/components/AgentWorkspace.vue) | 批量确认、筛选、详情请求生命周期 |
