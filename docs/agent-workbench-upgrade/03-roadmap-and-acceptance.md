# 路线图与验收

## 当前交付范围（2026-08-23）

本轮已收口「流式输出 + 多轮会话 + 工作台事件投影」：

- 创建者调试与发布 Web App 共用 Agent Protocol 工作台；
- 对话区显示完整多轮用户/Agent 消息，每轮内嵌可折叠执行过程；
- `values/messages/tools/lifecycle` 投影为回复、Todo、工具和同步子 Agent 事件；
- command 立即启动后台执行，SSE 只负责订阅，支持 Redis/DB 重放和取消；
- 发布页首轮任务使用 SDK thread UUID 创建真实会话，后续轮次沿用同一 `thread_id`；
- 刷新或重新打开任务时，从业务 Message 和 AgentRunEvent 恢复对话与执行过程。
- 已执行 `1df2bf5b63b9` 的数据库通过后继 revision 重建工作台 Run 表；
  LangGraph 托管表仍由 `PostgresSaver/PostgresStore.setup()` 自动创建。

下文 M7 的 HITL/异步子 Agent，以及 M8 的指标和灰度不属于本轮交付，
不影响当前流式与会话机制上线。

## 总体顺序

```text
M1 契约验证
  -> M2 Run/协议骨架
  -> M3 重放/安全
  -> M4 Vue 流式基础
  -> M5 创建者调试工作台
  -> M6 发布 Web App 工作台
  -> M7 异步子 Agent/HITL
  -> M8 灰度发布
```

每个里程碑必须单独完成验收和停走判断。上游协议或安全门禁未通过时，不得用 UI mock
掩盖问题后继续扩面。

## M1：事件与 SDK 契约验证

**前置条件**

- 当前 Deep Agents/LangGraph Python 依赖可运行；
- 可创建最小同步子 Agent fixture；
- 前后端各自有独立验证分支或工作树。

**交付物**

- 捕获 `stream_events(version="v3")` 的脱敏 fixture；
- 覆盖 root、Todo、tool、subagent、nested、parallel、file、error、interrupt；
- 记录 event name/data/tags/metadata/namespace 和顺序保证；
- 建立 `@langchain/vue` 最小页面，确认精确版本、peer dependencies；
- 记录 `HttpAgentServerAdapter` 实际 command/stream 请求与订阅契约；
- 形成 protocol compatibility matrix 和 ADR。

**验收**

- fixture 测试可重复执行且不访问生产资源；
- 能从原生事件确定父子作用域，不依赖文本解析；
- SDK 可以读取 root message 和至少一个 subagent scope；
- 明确 replay、interrupt、async subagent 尚未满足的部分；
- 文档中的候选端点/字段按实测结果更新。

**Stop/Go**

只有在官方 SDK 可由 Flask 兼容实现、作用域事件足以构建执行树时进入 M2。否则先评估
LangGraph 标准服务或极薄自定义 adapter，并记录原生方案为何失败，禁止直接扩写旧 SSE。

## M2：Run 模型与协议骨架

**前置条件**：M1 ADR 通过，协议版本锁定。

**交付物**

- `agent_run` 模型、迁移、repository/service；
- 创建者和发布态 command/stream 端点；
- `thread_id/run_id/event_id/sequence` 全链路；
- 从 GraphExecutionDriver 读取 v3 原生事件并完成最小协议投影；
- terminal status、错误码和最终摘要；
- 保持旧 `/chat` SSE 可用。

**验收**

- 单次 command 只创建一个 run；
- queued -> running -> terminal 状态合法且可审计；
- root/Todo/sync subagent/tool 事件在协议端可观察；
- 跨租户和跨 app 访问返回拒绝；
- 服务异常后 run 不永久停留在错误的 running 状态。

**Stop/Go**：协议契约测试、模型迁移回滚和旧 SSE 回归全部通过后进入 M3。

## M3：重放、顺序与安全投影

**前置条件**：M2 在单实例、不断线场景稳定。

**交付物**

- Redis Stream replay store、TTL、max length 和容量指标；
- `since`/`Last-Event-ID`、cursor expired snapshot/reset；
- 固定安全投影和服务端字段裁剪；
- 慢消费者、取消订阅和资源回收；
- 文件与 artifact 重新鉴权。

**验收**

- 任意断点重连无丢失、无重复、顺序稳定；
- 过期 cursor 有确定恢复路径；
- 浏览器无法获取系统提示、凭据或内部异常栈；
- 多实例下事件仍可恢复；
- Redis 不可用时按设计降级并留下可观测错误，不泄漏运行内部信息。

**Stop/Go**：重放混沌测试和安全用例通过后，前端才可以把协议作为正式数据源。

## M4：Vue 流式基础

**前置条件**：M3 协议稳定，SDK 版本锁定。

**交付物**

- 安装精确版本的 `@langchain/vue`、`@langchain/core` 及必要 SDK；
- `HttpAgentServerAdapter` 配置、鉴权和环境地址；
- workbench provider/composable、view model、selectors；
- reconnect、cursor reset、abort、error 状态；
- legacy/new protocol feature flag；
- fixture 驱动的组件测试。

**验收**

- root/Todo/subagent/tool/artifact 能从相同 run 正确选择；
- 运行中刷新和断网恢复不重复创建 command；
- 组件卸载后连接和订阅被释放；
- `npm run type-check`、单测和构建通过。

**Stop/Go**：数据层与页面容器解耦，fixture 覆盖嵌套/并行后进入 M5。

## M5：创建者调试工作台

**前置条件**：M4 共享数据层通过验收。

**交付物**

- 在编排页接入三栏调试工作台；
- Todo、scoped 时间线、tool detail、files/artifacts、run metrics；
- active/failed 自动展开和选择联动；
- 桌面、平板、移动响应式布局；
- 与旧 PreviewDebugChat 的开关回退。

**验收**

- 创建者能定位每个子 Agent 的消息、工具、错误和文件；
- 并行/嵌套结构不被压平成错误顺序；
- 长内容和大量事件不卡死、不遮挡；
- 目标视口截图和交互测试通过；
- 关闭 feature flag 即回到旧调试聊天。

**Stop/Go**：内部用户实测通过，且未发现协议缺字段问题后进入 M6。

## M6：发布 Web App 工作台

**前置条件**：M5 稳定；发布态安全评审通过。

**交付物**

- 应用发布配置增加 `progress/workspace`；
- Web App workbench shell 与共享组件接入；
- `progress` 的安全摘要视图；
- 公开 artifact 预览/下载；
- 旧应用默认迁移为 `progress`。

**验收**

- 旧应用不改配置时行为不变；
- 用户无法通过请求参数、浏览器状态或订阅范围提升可见性；
- 同一 run 在 creator/debug 与 published/progress 中呈现字段符合策略；
- 移动端可完成发起、观察、停止、查看结果全流程；
- Web App token、end-user、thread 和文件权限隔离通过。

**Stop/Go**：安全渗透用例和小流量真实任务完成率达标后进入 M7 或直接 M8。

## M7：异步子 Agent 与原生 HITL

**前置条件**：同步子 Agent 工作台稳定；checkpointer 可跨请求恢复。

**交付物**

- background task queued/running/completed/failed/cancelled 状态；
- 异步结果回填与跨 run/thread 关联策略；
- LangGraph interrupt 展示；
- `Command(resume=...)` 的允许/拒绝/编辑命令；
- 审批权限、幂等和审计记录。

**验收**

- 页面关闭后异步任务继续，返回后状态一致；
- 同一审批只能成功处理一次；
- 未授权主体不能查看或恢复 interrupt；
- 服务重启后可从 checkpoint 恢复；
- 取消、超时和失败状态不会被错误显示为完成。

**Stop/Go**：恢复、幂等和权限用例未通过时，保持 UI 只读或继续关闭功能。

## M8：灰度、观测与退场

**前置条件**：目标功能里程碑全部通过。

**交付物**

- 按 tenant/app 的 feature flag 与灰度名单；
- 指标、日志、trace 和告警面板；
- 容量/负载报告；
- 回滚手册、数据兼容说明；
- 旧 SSE 退场条件与时间表。

**关键指标**

- command 成功率、run 完成率和取消率；
- 首事件延迟、首 token 延迟、run 总耗时；
- 活跃 SSE 连接、断线率、重连成功率、cursor reset 率；
- 每 run 事件数/字节数、Redis 内存和积压；
- 前端长任务 CPU/内存、渲染错误率；
- 安全裁剪拒绝数和越权尝试。

**验收与回滚**

- 先创建者内部调试，再少量 Web App，再按 tenant 扩大；
- feature flag 能在不发版的情况下切回旧 SSE 页面；
- 新表和新事件不影响旧消费者；
- 回滚不删除 run/checkpoint 数据；
- 达到约定观察周期后才讨论旧链路下线，且下线需另立计划。

## 跨里程碑 Definition of Done

每个实现任务都必须满足：

1. 原生能力选择有依据，没有复制框架中间件或状态机；
2. 协议、模型、权限和 UI 类型同步更新；
3. 单元、契约、集成或 E2E 测试与风险匹配；
4. 新事件经过租户隔离与敏感字段审查；
5. 关键路径有日志、指标和稳定错误码；
6. 文档、`TODO.csv`、`PROGRESS.md` 随实现更新；
7. `git diff --check` 和两个仓库的静态校验通过；
8. 保留可验证的 feature flag 回退路径。

## 建议验证命令

命令需按实际虚拟环境与 package manager 调整，但结果必须记录到 `PROGRESS.md`。

Backend：

```powershell
Set-Location D:\spring_project\py\my-dify
python -m compileall internal/core/agent_v2 internal/service
pytest -q <本里程碑新增的精确测试路径>
git diff --check
```

Frontend：

```powershell
Set-Location D:\spring_project\py\my-dify-ui
npm run type-check
npm run test:unit -- --run
npm run build
git diff --check
```

协议和 E2E 应使用专门脚本/测试文件，禁止以人工“看起来能流式输出”代替重连、权限和
作用域断言。
