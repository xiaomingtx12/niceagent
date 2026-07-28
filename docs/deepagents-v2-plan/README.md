# deepagents-v2-plan

> my-dify 切换到 deepagents 框架的**拍板型规划文档**(唯一入口)。

## 文档索引

| 文档 | 用途 |
|---|---|
| [`00-harness-overall-upgrade.md`](00-harness-overall-upgrade.md) | 拍板型主轴文档:为什么切 + 从什么切到什么 + 5 期路线 R0-R4 + 硬约束 |
| [`01-business-flow-and-db-tables.md`](01-business-flow-and-db-tables.md) | 业务流程 + 数据库表盘点 + 新增/删除/修改表清单 |
| [`02-implementation-progress-and-roadmap.md`](02-implementation-progress-and-roadmap.md) | 当前实现进度、验证状态、未闭环功能与后续更新顺序 |
| [`03-context-memory-compression-implementation.md`](03-context-memory-compression-implementation.md) | 上下文装配、分层记忆、原生压缩的完整目标实现与验收规范 |
| [`04-skill-filesystem-backend-permission-plan.md`](04-skill-filesystem-backend-permission-plan.md) | 内置 Skill 公共目录、虚拟文件系统、Backend 路由、权限门控与受控脚本沙箱规范 |
| [`05-synchronous-multi-agent-implementation.md`](05-synchronous-multi-agent-implementation.md) | 同步多 Agent 当前实现、上下文隔离、工具/Skill/文件权限、事件、验收与异步阶段边界 |

## 文档原则

- **只反映当前拍板的真实状态**, 不保留废弃段、不打删除线、不写"已替换为 X"的历史注脚
- **遇到旧方案**: 直接删, 不留 changelog

## V1 当前交付边界

- Skill 只支持平台内置目录；Web Creator 可以查看目录中的 Skill，并把它们挂载到主 Agent 或显式配置的同步 SubAgent。
- SubAgent 只实现 DeepAgents 原生同步 `task` 委派，固定 `mode=sync`、`context_passing=NONE`。
- 异步 SubAgent、`SUMMARY/RECENT_K` 父历史复制和 HITL 暂不属于当前交付范围；API、配置和 UI 不得展示成可用能力。

## 项目级硬约束：框架原生优先

所有 Agent、记忆、上下文、文件系统、Skills、多 Agent、HITL 和持久化改造必须按以下顺序决策：

1. 先使用 DeepAgents 原生参数、中间件、Backend 和 Memory 能力。
2. DeepAgents 未覆盖时，再使用 LangChain / LangGraph 原生 Middleware、Checkpoint、Store、Interrupt 和 Callback。
3. 框架能力与 my-dify 数据模型不匹配时，只实现协议适配、权限隔离和业务持久化边界。
4. 只有前三级均无法满足且能给出明确缺失能力、替代方案评估和自动化测试时，才允许新增自有组件。

禁止包装或复制框架已有实现；禁止同时维护第二套摘要、token 裁剪、checkpoint、filesystem、Skills、Todo、sub-agent 或向量基础设施。

## 适用范围

- 后端: `D:\spring_project\py\my-dify` (deepagents-integration 分支)
- 前端: `D:\spring_project\py\my-dify-ui` (deepagents-integration-ui 分支)
