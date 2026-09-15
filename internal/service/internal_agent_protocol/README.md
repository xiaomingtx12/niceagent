# 内部子 Agent SDK 接口

本目录服务于已发布异步子 Agent 使用的 LangGraph SDK。它与前端工作台协议分开，但复用相同的业务 Run 生命周期。

- `auth.py`：签发和验证短期作用域凭证，构造内部 URL 与可信请求头。
- `service.py`：适配 SDK threads/runs 接口。签名在请求入口验证，账号和应用权限在每个业务入口复核一次。

创建 Run 直接返回 `InternalRunResp` 或 `RunRejected`，不再包装两个互斥可空字段。创建会话的数据库提交在方法内显式可见。线程查询只读取稳定会话时间、最新运行状态和最新一轮消息，不装配工作台历史事件；执行上下文仍由 Core checkpoint 完整保留。

`interrupt` 请求先解析输入，再交给 `AgentRunService.restart_run` 校验模型覆盖与附件归属。业务层请求取消旧执行，释放查询 Session 后等待数据库终态与本地资源完成；最多等待 10 秒，再沿用正常启动事务。超时返回 409 并明确新输入未启动，调用方可以稍后重试。这个接口不支持 rollback/enqueue，请求边界直接拒绝。

未知执行错误只返回固定公开提示，原始异常留在运行记录和日志。账号、应用和会话归属检查仍在任何状态恢复写入之前完成。

异步启动依次创建子会话和 Run；两次 HTTP 请求都成功后，Deep Agents 才将 `task_id`（子会话 ID）和 `run_id` 登记到父图的 `async_tasks`。`list_async_tasks` 只查询这份登记及其中 Run 的状态，不扫描平台所有后台执行。因此启动响应为 500、父图列表为空，不能证明后台没有创建或执行任务。

`_to_run_response` 同时用于启动回执和后续 Run 查询。SDK 的 `updated_at` 使用 `finished_at`，尚未结束时使用 `created_at`；`AgentExecution` 没有通用 `updated_at` 字段，不为适配 SDK 新增数据库列。2026-09-16 的五次异步启动曾在后台成功执行后，因这里读取已移除的字段而返回 500，导致父图没有登记已完成的任务；本次修正只恢复后续回执和查询，不改写已有父图 checkpoint。
