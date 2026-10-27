# Agent 流式输出代码阅读指南

上下文压缩新增阅读路径：`conversation_compaction.py`（主动工具）→ `conversation_history.py`（受控归档）→ `conversation_summary.py`（根图已提交状态）→ `AgentExecutor` 片段首尾确认 → `AgentEventNormalizer.committed_summary` → 原有 custom 投影。摘要模型结束仍由 `model_progress.py` 单独观察，两种事实不混用。前端 `ContextWindowInspector.vue` 按需调用会话 `/summary`，正文不参与 Token 流式更新。

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

### 6.4 流式展示如何避免阻塞点击

[buildWorkbenchTimeline](../../my-dify-ui/src/utils/workbench-timeline.ts) 先按 namespace 建立归属索引，再把事件分组为各父节点的活动、展示块和步骤。[WorkbenchExecutionTimeline](../../my-dify-ui/src/components/WorkbenchExecutionTimeline.vue) 将同一份索引传给嵌套时间线。子组件只读取自己的分组，正文增加时不再为每条事件反复扫描、排序整轮记录；同路径的归属候选仍按事件顺序选择，未绑定子图的委派不能认领兄弟活动。

[StreamingMarkdown](../../my-dify-ui/src/components/StreamingMarkdown.vue) 把正文交给 [markdown-renderer](../../my-dify-ui/src/utils/markdown-renderer.ts) 管理的共享 Worker。[markdown.worker](../../my-dify-ui/src/utils/markdown.worker.ts) 仍解析全文，以保留跨段引用、列表和未闭合 Markdown 的语义，但返回段落、列表项、表格单元格等组成的块树。Vue 复用未改变的 DOM，只有叶子 HTML 变化时才重写相应内容，不再每次替换整篇回答。

每个 Markdown 组件最多排队一份最新正文；更新更快时覆盖尚未开始的旧任务，不堆积逐 Token 的解析队列。已开始的解析可以先显示当前正文的前缀，正文被替换或清空后丢弃不匹配的旧结果。最后一个组件卸载时释放 Worker；线程不可用时显示经过 Vue 文本转义的原文，不回退到主线程全文解析。

[createAgentWorkbenchAdapter](../../my-dify-ui/src/services/agent-workbench.ts) 消费密集事件时，每处理约 8ms 让出一次浏览器任务队列。连续 `await` 已就绪的事件只会产生微任务，不能保证浏览器有机会处理点击；显式让出后仍按顺序继续，下一条事件重新检查会话与连接身份，切换页面后不再交付旧流。这个时间片约束的是事件之间的调度，不是单个大事件处理时长的硬上限。[ContextWindowInspector](../../my-dify-ui/src/components/ContextWindowInspector.vue) 的详细弹窗关闭后卸载内部内容，只有标题用量摘要继续更新。

2026-09-16 使用真实会话的 3052 条公开事件做集中到达回放时，曾观测到约 2.38 秒的连续主线程阻塞；这与单个约 37 KB、30 条合并事件的子 Run snapshot 回放不同，后者未复现持续卡死。密集事件调度与隐藏弹窗优化针对已复现的阻塞路径，不据此认定所有“页面点不动”都是同一原因。

2026-09-16 的本地开发模式浏览器诊断中，12 个子 Agent 展开、1000 条过程记录时，正文更新引起的点击等待从约 140–220ms 降至 6–12ms；2000 条记录从约 470–710ms 降至 9–13ms。约 15 万字符表格 Markdown 的更新期间，包含 Worker 回传和 DOM 更新的最大事件循环延迟约为 58ms。数据来自独立组件的模拟输入，不代表线上请求或整页性能承诺；超大内容的首次创建仍有 DOM 成本。

### 6.5 异步子 Agent 为什么单独显示

同步子 Agent 的过程属于父 Run 的嵌套事件，异步子 Agent 则有独立的 Run。将 `async_tasks` 当成同步子图渲染，会一直等待父流中不会出现的子任务输出，即使子任务早已完成。

派发时的任务名称与执行者名称是两个概念。[NamedAsyncSubAgentMiddleware](../internal/core/agent_v2/middleware/named_async_subagent_middleware.py) 扩展原生启动工具，接收可选的 `title`，缺少名称时从任务说明提取首句；在成功返回的 `Command` 中保存最终 `title` 和完整 `description`，`agent_name` 仍是配置中的执行者名称。Schema 和同步、异步执行函数都允许省略 `title`，避免展示字段阻断任务启动。原生工具重建任务记录时，状态合并器保留这两个描述字段，避免查询后名称消失。图装配仅向该中间件传递异步规格，同步规格继续交给 `create_deep_agent`，不重复注册异步工具。

阅读顺序：

1. [projectAsyncTaskEvents](../../my-dify-ui/src/utils/event-projection.ts) 将初始状态和实时 `values.async_tasks` 投影成 `async_task`，不混入同步 `subagent` 时间线。
2. [AgentConversationWorkbench](../../my-dify-ui/src/components/AgentConversationWorkbench.vue) 合并初始清单和已加载任务记录，跨轮去重，刷新无需加载父执行历史。
3. [AgentWorkspace](../../my-dify-ui/src/components/AgentWorkspace.vue) 提供顶部统一工作区入口，在同一面板内切换文件、后台任务和记忆。任务清单支持状态筛选与每次显示 30 条；批量确认真实子 Run 状态，终态停止轮询、隐藏暂停、离开释放请求。“查看详情”先调用轻量结果接口，结果上方的执行过程展开后才调用已有快照接口；列表和详情共用工作区任务页，返回、切换标签或关闭时取消详情请求。文件与记忆组件只在对应标签打开时挂载。
4. [AgentExecutionQuery](../internal/service/agent/agent_execution_query.py) 的 `get_run_statuses` 只读摘要列，`get_run_result` 读取已保存回答；两者只返回当前账号拥有且未删除会话中的 Run，不扫描事件表。

父图里的任务记录用来发现任务，不作为界面的实时状态来源。这里不自动唤醒父 Agent；模型再次查询任务仍使用自己的工具。

### 6.6 按需记忆与更新展示

[AgentMemoryMiddleware](../internal/core/agent_v2/middleware/agent_memory_middleware.py) 仅提供 delete_memory_file 工具，不加载记忆；固定使用规则由构图阶段加入系统提示。构图时 memory=None，原生文件工具仍通过 CompositeBackend 的 /memories/ 路由访问 [AgentMemoryBackend](../internal/core/agent_v2/backends/memory/agent_memory_backend.py)。从 ls/glob/grep 定位文件，再由 read_file 按需读取；已知路径可直接读取。没有自动目录、偏好注入或版本查询。

Backend 的实际写入、编辑或上传提交后，由 [publish_memory_update](../internal/core/agent_v2/execution/memory_updates.py) 发布 custom 事件；删除工具收到 Repository 的删除成功结果后发布同类事件。无变化编辑、相同内容上传、失败写入和删除不存在文件不发成功通知。管理页面通过 HTTP 响应确认，内容相同且未重命名时不更新时间。配额、事务锁与 expected_sha256 并发保护保留。

[AgentProtocolEventProjector](../internal/service/agent_protocol/event_projector.py) 用 [AgentMemoryUpdateResp](../internal/schema/agent/agent_memory_schema.py) 校验 type/path/action，身份沿用外层协议 event_id。前端 RunExecutionProjector 投影为 memory 记录，[MemoryUpdateNotice](../../my-dify-ui/src/components/MemoryUpdateNotice.vue) 按文件合并通知并提供内容入口，面板按新事件合并刷新。工具调用仍留在执行过程，不重复展示通知行，也不解析工具返回的自然语言。

其他会话的变更在下一次实际读取文件时可见，打开的面板另有低频补查；这两者都不代表模型每次调用前同步更新。已读取的旧工具消息和摘要可能仍在上下文，删除文件不等于擦除这些历史材料。

### 6.7 本轮规划选项与公开消息

[model_input.py](../internal/core/agent_v2/execution/model_input.py) 是模型输入与公开用户消息的区别所在。AgentExecutor 仅在新输入时调用 build_model_input，复制 HumanMessage 并加一个自动/显式规划选项块，保留 ID、正文和媒体。恢复时只传 Command(resume)，不再次追加。

[AgentEventNormalizer](../internal/core/agent_v2/execution/event_normalizer.py) 在完整用户消息转换成协议事件前移除内部块；[state_projector.py](../internal/service/agent_protocol/state_projector.py) 在状态投影时做相同处理。两处共用 public_input_message，检查服务端标记与首块一致，不按用户正文做字符串替换。checkpoint 留模型输入，公开消息留用户原文。

固定规则统一在 agent_system_prompt.py 装配。原生异步工具说明提前进入 system，原生 TodoList 等中间件继续负责各自工具语义。ask_user 的答案已经随工具返回进入历史，原先每次调用前查询和拼接全部近期答案的 RuntimeContextMiddleware 已删除。

## 7. 第二遍再读分支

第一遍读完上面的正常输出和刷新恢复流程即可。第二遍带着具体问题追分支：

| 问题 | 阅读入口 | 重点 |
| --- | --- | --- |
| 点击停止后，谁结束执行？ | `AgentExecutionService.cancel_run`、`_watch_cancel`、`AgentExecutor.request_cancel` | 取消请求如何传到执行器，图排空后如何保存结果。 |
| `ask_user` 为什么不算失败？ | [UserInputInterruptTracker](../internal/core/agent_v2/execution/user_input_interrupt_tracker.py)、`AgentExecutor._run_graph`、`AgentExecutionService._commit_result` | 正常中断如何被识别，并转成可提交的待填表单。 |
| 保存失败后为什么不能显示完成？ | `AgentExecutionEventBuffer.drain_and_close`、`AgentExecutionService._execute_run` | 事件尾批、失败补写和业务终态的提交顺序。 |
| 首个 Token 之前的“等待模型响应”从哪里来？ | [ModelProgressCallback](../internal/core/agent_v2/execution/model_progress.py) | 模型开始回调如何产生进度事件，并通过模型调用 ID 与正文关联。 |

每次阅读都围绕一个具体行为，沿调用关系追到结果。这样既能理解实现，也更容易发现哪些代码仍然表达得不够清楚。
