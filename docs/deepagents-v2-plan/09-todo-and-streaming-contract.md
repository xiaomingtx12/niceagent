# Todo 与流式事件契约

本文描述当前依赖 DeepAgents 0.6.12 时，原生 Todo 和 Agent 流式输出在
MyDify 中的适配边界。

## 1. 原生 Todo 能力

DeepAgents 原生 `TodoListMiddleware` 提供 `write_todos` 工具和 `todos` 状态
通道。一次更新是完整列表替换，每个条目只使用以下字段：

```json
{
  "content": "实现功能",
  "status": "pending"
}
```

支持的状态为：

- `pending`：尚未开始；
- `in_progress`：当前正在处理；
- `completed`：已经完成。

0.6.12 的原生能力不包含 Todo 依赖关系、优先级、截止时间、负责人、百分比
进度或独立的计划数据库。`task` 是 SubAgent 委派工具，不是 Todo 子任务
执行器。项目不新增第二套计划表或调度器。

Todo 更新规则：

1. 复杂任务由模型调用 `write_todos`；
2. 每次调用替换完整快照；
3. 同一模型轮次不允许并行调用多个 `write_todos`；
4. 成功的原生 `Command` 或 `updates.tools.todos` 才投影为一个
   `todo_update` 事件；
5. Checkpoint 使用稳定 `thread_id` 时，Todo 状态随原生图状态连续保存。

## 2. 流式输出

`GraphExecutionDriver` 默认请求 DeepAgents 0.6 的双流：

- `messages`：模型文本增量，投影为多个相同事件 ID 的 `agent_message`；
- `updates`：图状态更新，补取原生 Todo 快照；
- Middleware Hook：投影工具开始/结束和同步 SubAgent 委派边界。

模型完整响应不会和已经发送的增量重复。没有可用增量时，系统保留完整响应
作为回退。

## 3. 终止语义

`agent_end`、`error`、`stop`、`timeout` 是互斥终止事件。终止事件在 Backend
清理完成后由 `GraphExecutionDriver` 发布一次，前端不需要猜测连接关闭是否
代表成功。
