# 前端工作台实施计划

## 1. 产品形态

发布后的 Agent 和编排调试统一进入任务工作台。工作台保留完整多轮对话，每轮 Agent
回复关联一个可折叠执行过程；旧聊天协议只作为兼容链路保留。

工作台的核心用户问题是：

1. Agent 现在做到哪一步？
2. 哪个 Agent 正在做什么，是否失败或等待？
3. 已经生成了哪些可用文件或结果？
4. 是否需要我确认、补充或重试？

界面不展示“功能说明”和协议术语。状态通过计划、时间线、产物和操作自然表达。

## 2. 信息架构

### 2.1 桌面端

```text
┌──────────────────────────────────────────────────────────────────────┐
│ 应用名 / 会话状态                                  新会话  更多操作  │
├───────────────┬──────────────────────────────────┬───────────────────┤
│ 计划          │ 完整多轮对话                       │ 运行详情          │
│ □ 搜集资料    │ 用户消息                           │ 所选工具          │
│ ◐ 分析内容    │ ▼ 执行过程                         │ namespace         │
│ □ 生成报告    │ ├ 搜索工具 · 完成                   │ 参数 / 结果       │
│               │ └ researcher · 运行中              │                   │
│ 3 / 5         │ Agent 回复按 token 增长             │                   │
├───────────────┴──────────────────────────────────┴───────────────────┤
│ 输入框 / 附件 / 停止或发送                                            │
└──────────────────────────────────────────────────────────────────────┘
```

- 左栏：240px 左右，可折叠，展示 Todo、总体状态和失败定位。
- 中栏：最小 520px，自适应，承载完整对话与每轮折叠执行过程。
- 右栏：320px 左右，可折叠，只展示选中事件详情，不重复事件列表。
- 输入区固定在中间主工作区底部，不横跨侧栏。
- 面板通过细分隔线区分，不使用嵌套卡片、渐变或大面积装饰。

尺寸最终以常见 1366、1440、1920 宽度截图验收为准。窄桌面优先收起右栏，再收起左栏，
不能把中心时间线压缩到不可阅读。

### 2.2 移动端

- 完整对话作为主视图；
- 计划和产物从顶部图标按钮进入全高抽屉；
- 选中子 Agent/工具详情使用底部抽屉；
- 输入区避让安全区域和软键盘；
- 不做三列按比例缩小，不让长 tool 名称或文件名撑破容器。

## 3. 事件到视图状态

优先使用 `@langchain/vue` 提供的 `useStream`、消息、工具、interrupt 和 scoped/subagent
selectors。使用 `HttpAgentServerAdapter` 对接后端兼容端点，避免在浏览器继续维护第二套
SSE 状态机。

```text
HttpAgentServerAdapter
  -> Workbench stream provider
     -> root messages selector       -> 对话/最终回复
     -> Todo selector                -> 计划栏
     -> values/tools/lifecycle       -> 每轮折叠执行过程
     -> scoped subagent selectors    -> 子 Agent 作用域
     -> selected event               -> 右侧运行详情
     -> artifact projection          -> 产物栏
     -> connection/run state         -> 重连、停止、重试状态
```

引入前必须验证 `@langchain/vue` 与当前 Vue 3.4、TypeScript 5.4 和后端协议兼容，并锁定精确
版本。2026-08-22 调研时 npm 版本为 `1.0.32`，正式实施不得直接使用无上限版本范围。

### 3.1 规范化状态

工作台页面层只消费稳定的 view model：

```ts
type WorkbenchRun = {
  threadId: string
  runId: string
  status: 'idle' | 'queued' | 'running' | 'waiting' | 'completed' | 'failed' | 'cancelled'
  connection: 'connecting' | 'live' | 'reconnecting' | 'offline'
  lastSequence: number
}
```

子 Agent 节点以 namespace/path 为作用域键，父子关系来自协议，不从展示名或
`delegate/end` 文本猜测。消息、工具调用、错误和产物保留 scope，根视图按时间排序投影。

### 3.2 断线行为

- 网络中断后显示非阻塞重连状态，保留当前内容；
- 使用最后 `event_id/sequence` 恢复，补发事件不重复渲染；
- cursor 过期时拉取 state snapshot 并明确刷新本地投影；
- 页面刷新后根据 thread/run 恢复，而不是自动发起相同任务；
- 用户主动停止与网络断开必须显示为不同状态；
- 浏览器不可恢复时保留已完成内容，并提供重新连接/重试操作。

## 4. 核心组件

建议在前端新增共享工作台域，创建者和 Web App 只做容器与权限差异：

```text
src/features/agent-workbench/
  api/
    adapter.ts
  composables/
    useAgentWorkbench.ts
    useWorkbenchSelection.ts
  models/
    workbench.ts
  components/
    AgentWorkbench.vue
    WorkbenchHeader.vue
    WorkbenchPlanPanel.vue
    WorkbenchTimeline.vue
    WorkbenchTimelineItem.vue
    SubagentExecution.vue
    ToolExecution.vue
    WorkbenchArtifactsPanel.vue
    ArtifactPreview.vue
    ApprovalPanel.vue
    WorkbenchComposer.vue
    WorkbenchConnectionState.vue
```

与现有代码的关系：

| 现有文件 | 迁移策略 |
| --- | --- |
| `src/hooks/use-agent-chat-stream.ts` | 旧 SSE 保留；新页面改用 workbench composable |
| `src/models/agent-stream.ts` | 仅维护旧协议类型；不混入 v3 scoped state |
| `src/utils/agent-stream.ts` | 保持兼容 reducer；新协议由 adapter/selectors 消费 |
| `src/components/AgentThought.vue` | 旧聊天详情兼容，不再承载新工作台三栏 UI |
| `src/components/AiMessage.vue` | 复用消息渲染能力，避免嵌入完整执行树 |
| `PreviewDebugChat.vue` | 替换为创建者 `AgentWorkbench` 容器 |
| `src/views/web-apps/IndexView.vue` | 根据发布配置选择 chat/workbench shell |
| `WorkspaceFilesPanel.vue` | 抽取或适配为共享 artifacts panel |

不要一次删除旧组件。功能开关关闭或应用不满足新协议时，仍走现有聊天链路。

## 5. 组件行为

### 5.1 计划栏

- 使用框架 Todo 数据，不自建规划状态；
- 支持 pending、in progress、completed、failed/cancelled 显示；
- 始终只有框架真实 active 项，不由 UI 推断；
- Todo 很多时按当前项自动定位，列表本身可滚动；
- `progress` 模式只显示允许公开的文字或后端摘要。

### 5.2 每轮折叠执行过程

- 根 Agent、子 Agent、工具和 interrupt 使用不同的熟悉图标和状态标记；
- 并行子 Agent 在同一父节点下并列，不伪装成顺序步骤；
- 嵌套 Agent 可逐级展开，默认展开 active/failed，折叠已完成细节；
- token/message 增量更新内容，不创建大量重复 DOM 项；
- 工具执行先展示名称和状态，参数/结果进入详情区；
- 错误项提供稳定错误摘要和可执行操作，创建者 debug 才显示内部细节；
- 用户选择事件后，右栏切换到对应详情，不能打断对话流式滚动；
- 当前运行默认展开，完成后默认折叠，用户可随时重新展开；
- `write_todos` 和 `task` 分别投影为计划、子 Agent，不重复显示为普通工具。

### 5.3 产物栏

- 列表按文件/报告/链接等类型展示图标、名称、更新时间和状态；
- 预览与下载都重新经过服务端授权；
- 生成中的文件显示明确状态，不提供不可用下载；
- 同一 artifact id 更新时原位更新，避免重复项；
- 删除、覆盖或外部打开必须遵守发布端能力配置。

### 5.4 HITL 与命令

- interrupt 到达时运行状态为 `waiting`，审批卡紧邻相关执行项；
- 允许/拒绝/编辑仅使用后端给出的结构化 action schema；
- 提交后禁用重复操作，使用 `Command(resume=...)` 对应命令继续；
- 不把审批结果伪装成一条普通用户消息；
- 第一阶段仅预留组件和状态，Milestone 7 再开放生产交互。

## 6. 编排页与发布端

两处使用同一个组件、同一种对话结构和同一套事件投影。当前阶段不增加运行界面模式选择；
入口差异只保留业务路由和既有文件授权，固定脱敏在服务端执行。

## 7. 无障碍与性能

- 状态不只靠颜色表达；图标配可读文本或 `aria-label`；
- 所有图标按钮有 tooltip，键盘可聚焦；
- 新事件通知使用克制的 `aria-live`，不逐 token 播报；
- 面板展开、抽屉、审批和停止操作支持键盘；
- 时间线大于阈值后虚拟化，优先复用 `vue-virtual-scroller`；
- token delta 在 animation frame 或合理时间窗内批量提交；
- 自动滚动仅在用户处于底部时启用，用户向上浏览后不抢滚动位置；
- 固定面板和工具条尺寸，动态状态变化不得造成整体布局跳动。

## 8. 前端测试与视觉验收

### 单元/组件

- 重放去重与乱序防护；
- root/subagent/nested namespace 选择器；
- Todo 和 run 状态转换；
- artifact upsert；
- 固定安全投影后的渲染；
- reconnect、cursor reset、cancel、error、interrupt 状态；
- 长文本、长文件名、空状态和超长 Todo 列表。

### 端到端

- 同步单子 Agent 和并行/嵌套子 Agent；
- 运行中刷新页面并恢复；
- 人为断网后补发且无重复；
- creator 与 published 使用相同事件投影；
- 旧应用继续进入原聊天页面；
- 文件生成、预览、下载和越权失败；
- 后续 HITL 允许/拒绝/恢复。

### 视口

- Desktop：1366x768、1440x900、1920x1080；
- Tablet：1024x768；
- Mobile：390x844、430x932。

使用 Playwright 截图检查遮挡、溢出、固定输入区、抽屉和长内容。界面遵守现有暖灰、墨玉青、
细边框和扁平布局规范，不使用渐变、玻璃效果或卡片套卡片。
