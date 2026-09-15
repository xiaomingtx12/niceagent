# 工作台事件与恢复

本目录负责把 Core 事件转换为工作台事件，并把已保存事件合并为恢复材料。框架入口在 `internal/core/agent_v2/execution`；线程、数据库提交和 SSE 订阅在 `internal/service/agent`。

## 阅读顺序

1. `event_projector.AgentProtocolEventProjector.project_agent_event`：筛选公开事件、绑定本轮回答 ID、分配平台序号。
2. `agent_execution_service._execute_with_cancel_monitor` 中的 `deliver_event`：连接事件投影和缓冲交付。
3. `agent_execution_runtime.AgentExecutionEventBuffer.emit`：立即发布，独立写线程合批保存。
4. `agent_execution_service._execute_run`：执行退出后先 `drain_and_close`，再 `_commit_result`，最后回收本地登记。
5. `agent_execution_query._stream_events`：恢复已提交前缀，再接内存尾部；缓冲不覆盖游标时回数据库补齐。
6. `recovery_snapshot.compact_execution_events`：把前缀内容块合并为可直接显示的恢复材料。

## 一条事件的路径

```text
AgentEvent（已解析消息载荷）
  → projector.project_agent_event
  → AgentProtocolEvent（公开载荷、业务身份、平台序号）
  → buffer.emit
       ├─ 实时尾部 → 唤醒 SSE 订阅者
       └─ 有界写队列 → 独立线程批量保存

执行退出
  → 排空写队列并回收写线程
  → 同一事务保存回答、用量、运行状态、表单与终态事件
  → 提交成功后发布业务终态
```

`AgentExecutionEventBuffer` 不解释框架载荷，也不分配序号。`project_agent_event` 负责过滤根图终态、工具参数分片和未登记的 custom 类型，以及图状态适配和根助手消息 ID 绑定。过滤在编号之前完成。

## 哪些概念必须区分

| 概念 | 含义 |
| --- | --- |
| Core `sequence` | 单次执行片段内的交付序号，恢复执行重新开始，写入 `metadata.my_dify.source_seq`。 |
| 协议 `seq` | 由 projector 接续已保存游标分配，Core 事件和业务事件共用。 |
| 平台 Run ID | 一轮可暂停、可恢复的业务执行身份，用于事件 ID 和持久化归属。 |
| `model_run_id` | 一次模型调用身份；结合 namespace 和 node 隔离并行调用。 |
| 根图结束 | 框架退出，尚不能据此宣布业务结果已经保存。 |
| 业务终态 | 结果事务提交完成后发布，订阅可以据此结束。 |

业务终态批次顺序固定为：`values` → 每项 `input.requested` → `lifecycle`。不要在保存完成之前发布最后一项。

## 载荷解析和恢复合并

消息统一使用当前固定版本 `langchain-protocol` 的结构：

| 事件 | 唯一内容字段 | 操作 |
| --- | --- | --- |
| `content-block-start` | `content` | 登记真实内容块起点。 |
| `content-block-delta` | `delta` | 按 text/reasoning/data-delta 追加，或按 block-delta.fields 覆盖。 |
| `content-block-finish` | `content` | 用最终内容校正并关闭内容块。 |

`core/agent_v2/entity/message_event.MessageEvent` 按事件类别解析字段。旧 content 增量、不完整的消息顺序和未知增量格式不做兼容处理；历史数据也按当前协议读取，不补造缺失起点。

恢复合并按 `_ModelCallIdentity` → 内容块索引组织，每块保存真实起点、字段和待拼接文本分片。增量仅追加分片；结构字段覆盖时清除对应旧分片，结束帧直接校正完整内容。输出快照时才拼接尚未结束的块，并仅深拷贝最终保留的事件，原始序号和结束边界保留。模型调用 ID 可缺失于节点直接返回的完整消息，这属于当前 SDK 行为；分支和节点仍参与隔离。

`state_projector.adapt_graph_state` 负责回答 ID 和异步任务会话字段适配；`select_public_snapshot` 负责公开状态白名单。最终回答和阶段过程各有用途：回答从问答表恢复，阶段过程从事件前缀合并，不能相互替代。

## 历史与重连

工作台重连使用 `recovery: "snapshot"`：`_build_recovery_snapshot` 固定已提交水位，返回该前缀的合并材料，随后 SSE 交付更高序号的事件。历史详情读取 `/agent-runs/{run_id}/snapshot`；`/events` 分页与旧游标订阅保留兼容。

历史 SQL 在读取前裁掉工具参数分片和 values 中的重复消息，减少大载荷传输。它与 `MessageEvent.is_tool_argument_delta` 共用工具内容类型词表，只判断标准 `block-delta.fields.type`。

`parse_stored_event` 仅用于历史 JSON，custom 固定读取当前 name/payload 外壳并校验公开 Schema；Core 进度载荷固定带 type，实时投影负责一次包装，不再猜测原始和已包装两种输入。
