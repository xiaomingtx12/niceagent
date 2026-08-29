# Deep Agents V3 流式实现纠正记录

## 1. 纠正依据

本次实现以项目当前安装的 `deepagents 0.6.12`、`langgraph 1.2.9` 和 Deep Agents Event
Streaming v3 为准，并对照 Datawhale《DeepAgents 实战》第 14 章的生产交付建议。

当前方案没有改成另一套 Agent 运行时。Deep Agents 继续负责 Harness，LangGraph 继续执行图并
产生 V3 事件；项目的 Flask Adapter 只负责鉴权、业务 Run、协议投影、SSE、短期重放和历史
水合。

V3 原始事件的权威结构是：

```text
seq                    单次框架运行内严格递增的原始序号
method                 messages / values / tools / lifecycle 等通道
params.namespace       当前事件所在的完整图路径，[] 表示根图
params.timestamp       展示时间，不用于严格排序
params.data            由 method 决定的数据
```

`namespace` 是路由路径，不是子 Agent 展示名；流式事件也不会自动提供持久化、跨 worker 重放、
取消和背压，这些仍是 Adapter 的交付职责。

## 2. 本次发现的问题

| 问题 | 原因 | 直接影响 |
| --- | --- | --- |
| 子 Agent 完成被当成整个 Run 完成 | SSE 只检查 lifecycle 状态，没有检查 namespace | 跨 worker 订阅提前关闭 |
| 子 Agent 完成时关闭全部文本流 | Projector 没有按 namespace 管理收口范围 | 并行 Agent 的消息被提前 finish |
| 工具增量读取 `message` | V3 `tool-output-delta` 的正文位于 `delta` | 增量不显示或只剩最后一段 |
| 子 Agent 名称从路径猜测 | 路径段是运行标识，不是产品角色 | UI 可能显示 `tools` 等内部名称 |
| 框架原始 seq 丢失 | Adapter 只保留了 thread 全局 seq | 难以审计框架事件缺失和重排 |
| 每个 values 快照都写事件表 | 把状态快照当成了事件日志 | JSONB 重复写入，state 越来越慢 |
| state 带回 active Run 的 token 事件 | 内存 deque 未按 hydration 用途过滤 | 首次加载传输大量无用数据 |
| channel 历史重复订阅 | state 已水合历史，`useChannel` 仍启用 replay | 一次 command 出现额外 stream |
| 应用取消伪装成原生 `cancelled` | 框架状态和产品状态没有分层 | 协议来源不清晰 |

## 3. 后端协议纠正

### 3.1 两套序号同时保留

工作台仍使用 thread 连续的顶层 `seq` 作为 Redis 游标，因为多个 Run 必须共享一个不会回退的
浏览器顺序。同时在事件元数据中保留框架序号：

```json
{
  "seq": 418,
  "params": {
    "metadata": {
      "my_dify": {
        "source": "deepagents",
        "source_seq": 27
      }
    }
  }
}
```

一个原始消息事件可能被 Adapter 展开为 message start、content delta 和 message finish，因此
多条工作台事件可以拥有同一个 `source_seq`，但它们拥有不同的 thread 全局 `seq`。

由工作台补充的根 started/completed/failed 事件标记为 `source=workbench`，不会伪装成框架
原始事件。

### 3.2 namespace 决定生命周期作用域

Projector 会先规范化 lifecycle 中的 child namespace，再处理终态：

```text
根 namespace [] 终态
  -> 收口所有仍未 finish 的消息流

子 namespace ["tools:..."] 终态
  -> 只收口该 namespace 及其后代消息流
  -> 不影响协调 Agent 和其他并行子 Agent
```

跨 worker SSE 只有在 `params.namespace == []` 且应用状态进入
`completed/failed/cancelled` 时才结束当前 Run。子 Agent 的 completed、failed、interrupted 或
drained 只更新对应子 Agent，不再关闭根订阅。

### 3.3 框架生命周期与应用状态分开

LangGraph 当前 lifecycle transformer 负责产生子图生命周期，根 Run 的业务生命周期仍由工作台
补充。取消使用下面的扩展语义：

```json
{
  "method": "lifecycle",
  "params": {
    "namespace": [],
    "data": {
      "event": "interrupted",
      "status": "cancelled",
      "reason": "cancel_requested"
    }
  }
}
```

`event` 表达执行被中断，`status` 表达产品 Run 已取消。前端优先识别应用 `status`，数据库仍以
`AgentRun.status=cancelled` 为权威。运行异常的详细文本只写服务器日志和内部错误摘要，浏览器
事件不再直接暴露 `str(exc)`。

## 4. 前端投影纠正

### 4.1 工具增量按调用 ID 累积

前端以 `tool_call_id` 建立一次工具调用的临时累积器：

```text
tool-started       -> 保存名称和输入，清空 delta
tool-output-delta  -> 按到达顺序追加 data.delta
tool-finished      -> 使用最终 output；缺失时回退累计 delta
tool-error         -> 使用错误 message 并进入失败状态
```

字符串 delta 直接拼接，结构化 delta 按顺序序列化，后到事件只更新同一工具行，不新增重复行。

### 4.2 子 Agent 名称不再从 namespace 猜测

`task` 工具本身仍不作为普通工具行展示，但它的 `input.subagent_type` 会按 `tool_call_id` 保存。
子 Agent started lifecycle 携带的 `cause.tool_call_id` 用来完成关联：

```text
task.tool_call_id
  -> task.input.subagent_type
  -> lifecycle.cause.tool_call_id
  -> namespace 对应的子 Agent 展示名称
```

如果旧事件没有 cause，再回退到框架提供的 `graph_name`；namespace 最后一段只作为最终兜底，
不再作为首选名称来源。

### 4.3 state 水合与实时订阅不重复

历史执行事件已经由 `state.values.workbench_events` 返回，因此组件的 `useChannel` 使用
`replay: false`，只消费共享实时流。这样消除了同一批 values/tools/lifecycle 历史的额外 replay
订阅，但不会合并 SDK 为根内容和全 namespace 生命周期建立的不同用途连接。

## 5. 数据库存储与 state 性能

修正后的数据分工是：

| 数据 | Redis Stream | `agent_run` | `agent_run_event` | state |
| --- | --- | --- | --- | --- |
| message token | 短期完整保存 | 不保存 | 不保存 | 不返回历史 token |
| 最新 values | 短期保存 | 覆盖 `state_snapshot` | 不追加 | 每个 Run 最多合成一条摘要 |
| tools | 短期保存 | 不保存正文 | 追加 | 当前消息页关联事件 |
| lifecycle | 短期保存 | 状态字段 | 追加 | 当前消息页关联事件 |
| 最终问答 | 不作为长期权威 | 关联 Message | 不重复保存 | 从 Message 分页返回 |

`state` 现在只返回当前消息页关联 Run 的工具、生命周期事件和每个 Run 的最新 Todo/文件摘要。
活动 Run 的 messages token 和重复 values 不再进入 `workbench_events`。已有数据库中的旧 values
事件也会在查询时被过滤，不要求修改表结构或生成迁移文件。

Redis 的职责没有改变：它是低延迟共享和短期断线重放层，不是业务状态权威。Redis 丢失后，
数据库仍可恢复 Run 状态、最新快照、工具和生命周期，但已经过期的 token 增量不会被长期补回，
最终回答以 Message 为准。

## 6. 尚未扩大的协议范围

本次没有直接开放任意 `custom` 和 `input` payload。原因是这些数据形状由应用定义，直接放行会
绕过工作台的稳定 Schema 和敏感字段边界。接入 HITL 时应单独定义 interrupt/command Schema、
恢复接口和前端状态，再把相应通道加入公开协议。

当前生产者仍是 Flask 进程内后台线程。进程被强制终止时，Redis 和 PostgreSQL 能保留已发生的
事件，但没有官方 Agent Server 或可靠任务队列负责接管未完成运行。这是运行调度能力边界，
不能通过继续增加 SSE 逻辑解决。

## 7. 校验结果

本次修改不涉及 ORM 字段和数据库迁移。完成了以下校验：

- 后端修改文件通过 Python `compileall`；
- PostgreSQL JSONB `payload ->> method` 过滤表达式可正常编译；
- 前端通过 `vue-tsc --build --force`；
- 后端和前端目标文件通过 `git diff --check`。

后续升级 Deep Agents 或 LangGraph 时，应重新核对 lifecycle 状态、tools payload、namespace
形状和 typed projection 名称来源，再更新 Adapter 契约。
