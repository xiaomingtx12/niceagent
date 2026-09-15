# Agent 流式输出代码阅读指南

沿着“一次回答如何产生、显示、保存、恢复”阅读。先掌握主流程，再读取消、表单和异常分支。

整套代码主要处理三件事：

```text
产生：框架执行 → 规范化事件 → 工作台事件
交付：工作台事件 → 实时发送，同时异步保存
恢复：读取已保存事件 → 合并内容 → 接上后续实时事件
```

## 1. 从执行总入口看整体顺序

先打开 [AgentExecutionService._execute_run](../internal/service/agent/agent_execution_service.py)。第一遍只看下面四个动作，这段代码是阅读用的简化示意：

```python
result = self._execute_agent(...)  # 执行，期间不断产生事件
events.drain_and_close()           # 等待事件保存完成
self._commit_result(...)          # 保存最终回答、用量和状态
self.tracker.finish(run)           # 清理进程中的执行登记
```

这是整条链路的主干。周围的 `try/except/finally` 用于确保任何失败路径也会进行结果结算、释放资源。

发送消息和接收事件是两个请求：`/commands` 启动后台执行并返回 Run ID，`/stream` 订阅这个 Run。浏览器断开订阅，后台执行仍可继续。

## 2. 理解一个回答长什么样

假设模型回答“你好”，消息通道大致产生：

```text
message-start          role=ai，开始一条消息
content-block-start    index=0，content.text=""
content-block-delta    index=0，delta.text="你"
content-block-delta    index=0，delta.text="好"
content-block-finish   index=0，content.text="你好"
message-finish         这条模型消息结束
```

一条消息可以包含多个内容块，例如：

```text
index=0：思考
index=1：正文
index=2：工具调用
```

[MessageEvent](../internal/core/agent_v2/entity/message_event.py) 负责解释这些字段。阅读时关注两个方法：

- `parse()`：按照事件种类读取 `content` 或 `delta`。
- `TextDelta` / `FieldDelta`：分别表达追加文本和覆盖字段，恢复时由 `_ContentBlock.append()` 累计。

开始和结束事件使用 `content`，增量事件使用 `delta`，这是同一套当前协议中不同事件的结构。

`message-finish` 只表示一条模型消息结束。Agent 后面可能还要执行工具、再次调用模型。

## 3. 跟踪事件如何离开框架

接着读 [AgentExecutor._run_graph](../internal/core/agent_v2/execution/agent_executor.py)。主循环可以理解为：

```python
for raw_event in stream:
    # 收集最终图状态
    # 处理取消和正常中断
    for event in normalizer.accept(raw_event):
        on_event(event)
```

这里有两条输出：

| 输出 | 用途 |
| --- | --- |
| 连续调用 `on_event` | 让用户及时看到正文、思考和工具过程。 |
| 最后返回执行结果 | 交给业务层保存最终回答、状态和用量。 |

[AgentEventNormalizer.accept](../internal/core/agent_v2/execution/event_normalizer.py) 把框架输出变为 [AgentEvent](../internal/core/agent_v2/entity/agent_event.py)，包括过滤内部摘要、转换完整消息、保留调用身份和分配交付序号。

此时还没有写数据库，也没有发送 HTTP。

## 4. 理解框架事件如何变成工作台事件

读 [AgentProtocolEventProjector.project_agent_event](../internal/service/agent_protocol/event_projector.py)。这个方法集中做四件事：

1. 过滤根图终态，等待业务结果真正保存后再通知完成。
2. 过滤不展示的工具参数分片和未登记的自定义事件。
3. 给根助手消息绑定本轮业务回答 ID。
4. 分配平台序号，构造 `AgentProtocolEvent`。

相关身份需要分清：

| 字段 | 表示什么 |
| --- | --- |
| Run ID | 整轮 Agent 执行。 |
| `model_run_id` | 这一轮中的某次模型调用。 |
| `namespace` | 主图或子图所在分支。 |
| `node` | 产生事件的图节点，参与消息隔离。 |
| `index` | 一条消息内部的内容块位置。 |
| Core `sequence` | 当前执行片段的事件序号。 |
| 协议 `seq` | 工作台使用的事件序号，恢复执行后继续递增。 |

表单恢复仍属于同一 Run，但会启动新的执行片段，因此两种序号不能混用。

## 5. 理解实时发送和保存如何并行

读 [AgentExecutionEventBuffer.emit](../internal/service/agent/agent_execution_runtime.py)，再看同文件的 `_run_writer()`。

```text
emit(event)
  ├─ 发布到 EventHub，让订阅者立即读取
  └─ 放入待写队列，让独立线程批量保存
```

这几个对象各自保存不同的信息：

| 对象 | 职责 |
| --- | --- |
| Tracker | 当前哪些会话正在执行。 |
| Buffer | 哪些事件还需要保存。 |
| EventHub | 最近可供订阅者读取的实时事件。 |
| 数据库 | 已经提交的历史事件。 |

[EventHub.read_after](../internal/service/agent/agent_execution_event_hub.py) 的返回值很关键：

- 返回非空事件列表：有新内容。
- 返回 `[]`：订阅者已经追上，等待下一批。
- 返回 `None`：所需历史已经不在内存中，需要查数据库。

然后读 [AgentExecutionQuery._stream_events](../internal/service/agent/agent_execution_query.py)：它在内存读取、数据库补齐、等待通知之间切换，通过 `yield` 交付事件，最后由 [sse_response](../pkg/response/response.py) 编码成 SSE 响应。

正常结束订阅后，SSE 写入 `: stream-end` 注释。前端只扫描这个短标记来识别异常 EOF，业务 JSON 交给 SDK 解析一次；运行是否完成仍由快照和根生命周期事件决定。

## 6. 理解完成与刷新恢复

### 6.1 什么时候才算完成

执行结束后，[AgentExecutionService._commit_result](../internal/service/agent/agent_execution_service.py) 保存最终回答、用量、状态和终态事件，提交成功后发布通知。

这里必须记住：

> 正文已经显示，不代表它已经保存；模型结束，也不代表整轮 Agent 已经完成。

最终聊天回答由 [extract_final_answer](../internal/core/agent_v2/execution/message_text.py) 从本轮图状态中提取。执行过程中的多段输出则保留在事件中，因此“最终回答”和“阶段过程”有不同的数据来源。

### 6.2 刷新后如何接上实时流

刷新时，先读 [AgentExecutionQuery._build_recovery_snapshot](../internal/service/agent/agent_execution_query.py)。假设数据库已保存到序号 `100`：

```text
固定水位 100
  → 读取并合并序号 ≤ 100 的事件
  → 返回恢复材料
  → SSE 从序号 > 100 继续发送
```

“水位”就是这份恢复材料所覆盖的最后一个已提交事件序号。构造恢复材料期间产生的新事件，留给后续 SSE 交付。

再读 [compact_execution_events](../internal/service/agent_protocol/recovery_snapshot.py)。它按模型调用和内容块索引处理：

```text
开始：登记内容块
增量：累加内容，更新原起点的展示内容
结束：用完整内容校正，释放内容块
```

例如刷新时“你好”已经产生，恢复材料可以直接让起点带上“你好”，省去重新播放“你”“好”两个增量。数据库原始事件不变。

### 6.3 前端如何使用这些事件

前端对应的阅读入口是 [projectMessage](../../my-dify-ui/src/utils/event-projection.ts)：开始、结束用完整内容赋值，增量追加文字，思考和正文分别保存。

同文件中的 `RunExecutionProjector` 管理各 Run 的事件集合、去重和展示投影。[recoverySeedEvents](../../my-dify-ui/src/services/workbench-recovery.ts) 为 SDK 重建接收后续增量所需的消息上下文。[createAgentWorkbenchAdapter](../../my-dify-ui/src/services/agent-workbench.ts) 将订阅、恢复材料和 SDK 连接起来。

## 7. 第二遍再读分支

第一遍读完上面的正常输出和刷新恢复流程即可。第二遍带着具体问题追分支：

| 问题 | 阅读入口 | 重点 |
| --- | --- | --- |
| 点击停止后，谁结束执行？ | `AgentExecutionService.cancel_run`、`_watch_cancel`、`AgentExecutor.request_cancel` | 取消请求如何传到执行器，图排空后如何保存结果。 |
| `ask_user` 为什么不算失败？ | [UserInputInterruptTracker](../internal/core/agent_v2/execution/user_input_interrupt_tracker.py)、`AgentExecutor._run_graph`、`AgentExecutionService._commit_result` | 正常中断如何被识别，并转成可提交的待填表单。 |
| 保存失败后为什么不能显示完成？ | `AgentExecutionEventBuffer.drain_and_close`、`AgentExecutionService._execute_run` | 事件尾批、失败补写和业务终态的提交顺序。 |
| 首个 Token 之前的“等待模型响应”从哪里来？ | [ModelProgressCallback](../internal/core/agent_v2/execution/model_progress.py) | 模型开始回调如何产生进度事件，并通过模型调用 ID 与正文关联。 |

每次阅读都围绕一个具体行为，沿调用关系追到结果。这样既能理解实现，也更容易发现哪些代码仍然表达得不够清楚。
