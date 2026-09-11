# Agent 服务阅读入口

从 `agent_workbench_service.py` 的公共方法开始阅读。文件前半部分是业务入口，后半部分是内部实现。

| 文件 | 职责 |
| --- | --- |
| `agent_workbench_service.py` | 启动、取消、提交答案及重新填写；编排业务规则和入口事务 |
| `agent_workbench_query_service.py` | 工作台状态、模型选项、历史事件和 SSE 订阅，只读已提交数据 |
| `agent_model_service.py` | 默认模型与手动覆盖的选择规则、连接授权、思考强度和模型目录 |
| `agent_definition_service.py` | 读取草稿／发布／内置定义，确定默认模型来源和恢复指纹 |
| `agent_assembly_service.py` | 消费应用定义，绑定模型和已授权能力，交给 Core 装配一次性执行器 |
| `agent_workbench_execution_service.py` | 有界后台调度、心跳、取消监控、Agent 执行与结果交付 |
| `agent_workbench_run_service.py` | Run、事件、消息的数据库读写，以及执行租约和失联恢复 |
| `agent_workbench_event_buffer.py` | 当前执行的事件批量落库和首字交付 |
| `agent_workbench_event_queue.py` | 进程内运行登记、取消信号和订阅通知 |
| `creator_task_service.py` | 创建者任务页的应用与会话查询、更新、删除 |
| `assistant_agent_service.py` | 内置助手的会话入口和能力定义 |
| `agent_attachment_service.py` | 附件归属校验与运行空间写入 |
| `agent_conversation_cleanup_service.py` | 会话、消息及应用关联运行资源的清理 |

## 一次对话

1. `start_run` 锁定会话，检查重复请求和冲突，创建消息及 Run。
2. 事务提交后调用执行服务的 `launch`；执行服务调用装配服务创建当前 Run 的 Agent。
3. Core 事件经 `agent_protocol` 投影，再由事件缓冲写入数据库。
4. `_finish_run` 依次处理结果状态、最终快照、原子落库和提交通知。
5. 查询服务的 `subscribe` 读取已提交事件；进程内通知只是唤醒机制，数据库是重连的数据源。

## 待填写与取消

- `respond_input` 复核答案、身份和配置指纹，提交后才恢复图执行。
- `reopen_input` 只恢复已取消表单，不启动图，也不领取执行租约。
- `request_cancel` 对活动运行请求停止；没有执行线程的待填运行直接原子取消。
- 同一会话的操作先锁会话，再锁 Run；重构时保留各入口原有的 Run 查询范围。
- `internal/entity/agent_run_interaction.py` 集中维护答案、重复提交、取消批次和恢复快照规则。
- `queue_resumed_run`、`reopen_pending_run` 在调用方事务内保存状态、快照及执行租约，命令服务不直接修改这些字段。

## 修改时的边界

- 应用配置来源与恢复指纹由定义服务管理，模型覆盖规则由模型服务解析；查询和运行命令不依赖装配服务。
- 消息、表单、生命周期的协议结构集中在相邻的 `agent_protocol` 目录。
- 事件只能在提交后通知；租约失效的执行实例不能覆盖后续结果。
- 已发布子 Agent 的模型由其发布配置决定，自定义跟随子 Agent 绑定本次父模型。
- SDK 的命令 ID、分派及成功／错误外壳由 `agent_protocol/command_adapter.py` 处理，业务命令返回 `RunAccepted` 或 `RunRejected`。
- `EventBatch` 是内部扫描结果，保留扫描游标，不再伪装成 HTTP 响应 Schema。

## 数据对象的位置

- `internal/entity/agent_invocation.py`：可信调用身份和可恢复输入；保持历史 JSON 字段格式，供业务服务和运行存储使用。
- `internal/schema/agent/agent_model_schema.py`：工作台模型选项的公开响应。
- `internal/schema/agent/agent_cleanup_schema.py`：应用、会话和消息的清理请求。
- `ApplicationAgentDefinition`：一次读取的应用名称、所有者和配置，替代需要记住字段位置的四元组。
