# Agent 工作台升级计划

## 文档目标

本目录把 Deep Agents 流式传输、事件流和官方前端能力转化为可执行的
平台升级计划。范围覆盖两个仓库：

- Backend：`D:\spring_project\py\my-dify`
- Frontend：`D:\spring_project\py\my-dify-ui`

本目录既记录实施基线，也记录已经落地的运行时改造。后续实现以本目录和
`.codex-tasks/20260822-agent-workbench-upgrade/` 为准，不依赖聊天记录。

## 结论

已发布 Agent 的使用形态应从“带少量执行详情的聊天窗口”升级为“任务工作台”。
聊天仍是任务输入和结果交付入口，但规划、子 Agent 协作、工具调用、文件产物和
人工确认应成为一等信息，而不是继续塞进单条消息气泡。

2026-08-23 需求收敛：当前阶段只完成 Deep Agents 原生流式事件输出与工作台投影，
不继续扩展可见模式、单工具展示策略、异步子 Agent、HITL、指标或新的任务模型。

最终桌面布局：

```text
┌──────────────┬────────────────────────────────┬──────────────────┐
│ 计划 / Todo  │ 完整多轮对话                     │ 运行详情           │
│ 当前进度     │ 每轮可折叠的执行过程与 Agent 回复  │ 所选事件参数和结果  │
└──────────────┴────────────────────────────────┴──────────────────┘
```

编排页和发布 Web App 共享同一个工作台组件与事件投影。当前不提供
`debug/progress/workspace` 产品选项；服务端保留固定敏感字段保护，避免凭据、系统提示词
和内部异常栈进入浏览器事件流。

## 架构原则

1. Deep Agents 继续负责 Harness、Todo、SubAgent、文件系统和记忆能力。
2. LangGraph 继续负责运行、检查点、事件流、线程和中断恢复。
3. 优先采用 Agent Streaming Protocol 与 `@langchain/vue`，不另造一套浏览器事件模型。
4. my-dify 当前只负责业务路由、基础鉴权、固定脱敏和原生事件协议投影。
5. 旧 Queue/Thread/EventSession、旧 SSE 投影和 `task_id` 取消链均已删除；所有 Agent 入口统一消费 Agent Protocol。
6. 先支持同步子 Agent 的完整作用域流；异步子 Agent 和原生 HITL 后置。

## 当前交付范围

- 后端输出 `messages/values/tools/lifecycle` 四个原生通道；
- 每个事件保留 `event_id/seq/namespace/timestamp`；
- `messages` 驱动完整多轮对话和 Agent 文本增量；
- `values.todos`、工具和子 Agent 生命周期进入对应轮次的折叠执行过程；
- 工具开始、完成和失败原位更新，不生成重复事件行；
- `write_todos`、`task` 由计划和子 Agent 投影展示，不重复显示成普通工具；
- 调试、WebApp、辅助 Agent、OpenAPI 和微信后台调用统一创建 `AgentRun`，不再使用旧 `/chat` SSE 字段。

## 文档导航

- [流式协议与后端计划](./01-streaming-protocol-and-backend.md)
- [前端工作台计划](./02-workbench-frontend.md)
- [路线图与验收](./03-roadmap-and-acceptance.md)
- [M1 契约 ADR](./04-m1-contract-adr.md)
- [流式架构升级与运行原理](./05-streaming-implementation-and-lessons.md)
- [V3 流式实现纠正记录](./06-v3-streaming-corrections.md)
- [旧流式执行链删除说明](./07-legacy-stream-removal.md)
- [Workspace 存储与 Sandbox 同步纠正说明](./08-workspace-storage-and-sandbox-corrections.md)
- [Agent 分层记忆机制纠正说明](./09-layered-memory-corrections.md)
- [执行规格](../../.codex-tasks/20260822-agent-workbench-upgrade/SPEC.md)
- [任务清单](../../.codex-tasks/20260822-agent-workbench-upgrade/TODO.csv)
- [当前进度](../../.codex-tasks/20260822-agent-workbench-upgrade/PROGRESS.md)
- [总体验收](../../.codex-tasks/20260822-agent-workbench-upgrade/ACCEPTANCE.md)

## 官方资料基线

- 项目中文资料入口：[事件流](https://www.deepagent.net.cn/guide/event-streaming)、
  [流式传输](https://www.deepagent.net.cn/guide/streaming)、
  [前端总览](https://www.deepagent.net.cn/guide/frontend-overview)
- [Deep Agents Event Streaming](https://docs.langchain.com/oss/python/deepagents/event-streaming)
- [Deep Agents Streaming](https://docs.langchain.com/oss/python/deepagents/streaming)
- [Deep Agents Frontend Overview](https://docs.langchain.com/oss/python/deepagents/frontend/overview)
- [Subagent Streaming](https://docs.langchain.com/oss/python/deepagents/frontend/subagent-streaming)
- [Todo List](https://docs.langchain.com/oss/python/deepagents/frontend/todo-list)
- [Sandbox / Files](https://docs.langchain.com/oss/python/deepagents/frontend/sandbox)

实施 Milestone 1 时必须重新核对已安装 Python 包和锁定的 npm 包版本，因为官方协议
和前端包仍可能演进。本文档记录的是 2026-08-22 的决策基线。
