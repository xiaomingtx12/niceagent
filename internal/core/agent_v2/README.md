# Agent V2 包结构

`agent_v2` 是现有 Flask 应用接入 Deep Agents 和 LangGraph 的核心适配层。它不实现第二套
Agent 运行时，只负责把项目业务配置、身份、文件能力和协议边界接到框架提供的能力上。

## 目录职责

```text
agent_v2/
|-- entity/                 Agent V2 私有领域对象和值对象
|-- schema/                 Agent V2 私有边界校验与传输结构
|-- execution/              单次图执行和运行资源生命周期
|   |-- agent_executor.py   单次图执行入口
|   `-- run_resources.py    单次 Run 的技术资源集合
|-- protocol/               v3 原始事件到 Agent Protocol 的投影
|-- assembly/               Deep Agents 图构建和运行配置
|   |-- graph/              图构建、Harness 和 SubAgent 规格适配
|   |-- configuration/      提示词和线程配置
|   |-- context/            Workspace、Memory、Skill、Sandbox 上下文
|   `-- observability/      遥测和 Token 用量框架适配
|-- backends/               Deep Agents Backend 实现
|-- middleware/             模型调用与审查中间件
`-- persistence/            LangGraph Checkpointer、Store 和文件仓储
```

## 依赖方向

```text
service
  -> execution
  -> assembly
       -> assembly/context
       -> backends
       -> persistence
  -> protocol

请求级资源总装配由 ``internal.service`` 中的 Agent Assembly Service 负责；Core 只提供
可组合的 Agent 运行时组件。

execution/assembly/protocol/backends/persistence
  -> entity/schema
```

`entity` 和 `schema` 不反向依赖执行层。`protocol` 不负责运行 Agent，`execution` 不拼接
HTTP/SSE 响应，`backends` 不保存业务 Run 状态。PostgreSQL 中的 `AgentRun` 仍由工作台
Service 管理，Redis 重放也不属于本包。

## 新代码放置规则

- 新增 Agent Protocol channel 或事件字段投影，放入 `protocol`。
- 新增一次运行需要的 Backend 组合或上下文，放入 `assembly/context`。
- 新增 Deep Agents/LangGraph 原生 Backend，放入 `backends`。
- 新增 Checkpointer、Store 或文件持久化实现，放入 `persistence`。
- 新增纯状态、值对象和领域规则，放入 `entity`。
- 新增 Agent V2 内部边界输入校验，放入 `schema`。
