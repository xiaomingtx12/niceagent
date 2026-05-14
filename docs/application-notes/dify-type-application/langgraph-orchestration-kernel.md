---
sidebar_position: 2.2
---

# 平台的 LangGraph 编排骨架

如果说 LangChain 提供的是零件库，那 LangGraph 在这个项目里更像执行骨架。

它不负责页面、不负责权限、不负责发布门禁，但 Workflow 和 Agent 这两条最核心的运行链路，都靠它来承接状态、路由、循环和归并。

## LangGraph 在这里不是“工作流画布”

很多人一提 LangGraph，就只想到可视化工作流。

放回这个项目里看，它至少落在两个位置：

- Workflow：把前端画布 DSL 编译成可执行图
- Agent：把提示词预处理、记忆注入、模型推理、工具循环编成运行骨架

所以这一篇更关注 LangGraph 在平台里的“内核角色”，而不是某个单页面功能。

## 我会把它拆成六个部分

### 1. 状态模型

- `StateGraph` 的前提不是节点多，而是先把共享状态定义清楚
- Workflow 侧的 `inputs`、`outputs`、`node_results`、`intent_condition` 对应不同 reducer 规则
- Agent 侧则围绕消息状态展开，用消息归并承接多轮推理和工具调用
- 这决定了节点只声明“我改了什么”，不用反复重建整份状态

### 2. 图构建

- LangGraph 真正关心的是节点、边、条件边和起止点
- 节点可以是函数，也可以是 runnable；Workflow 选了类节点体系，Agent 选了成员函数体系
- 编译前是 `StateGraph` builder，编译后才是可执行图
- 这让平台可以把“配置阶段”和“运行阶段”明确拆开

### 3. Workflow 编译链

- 前端传来的不是 LangGraph 对象，而是一份 `nodes + edges` 业务 DSL
- 后端先经过宽校验保存草稿，再通过 `WorkflowConfig` 做严格校验和强类型化
- 真正运行前，DSL 才会被编译成 `StateGraph`
- 这一步把“画布编辑”变成“可执行工作流”，也是平台最关键的工程边界之一

### 4. Agent 执行骨架

- Agent 侧不是让模型自由发挥，而是把预设处理、长期记忆召回、LLM、工具节点和退出条件编成一张固定图
- Function Calling 和 ReAct 不是两套完全不同的系统，而是在同一张骨架上替换局部实现
- 这让平台既保留了工具循环能力，又没有把运行时控制散落到 service 代码里

### 5. 事件流与运行时外壳

- LangGraph 负责图内编排，不直接等于平台对外输出
- Workflow 侧可以更直接地消费 `stream()` 做调试
- Agent 侧还要再包一层事件队列，才能支持前端推理可视化、SSE、停止和错误事件
- 这说明 LangGraph 是编排内核，不是整个平台运行时的全部

### 6. 发布与复用边界

- Workflow 不只要“能跑”，还要区分 `draft_graph` 和发布态 `graph`
- 开始节点输入会被映射成工作流对外暴露的 `args_schema`，所以工作流最终能再作为 `BaseTool` 注入 App / Agent
- 这一步把 LangGraph 图从“内部执行结构”进一步变成“平台能力单元”

## 这里最容易理解错的四件事

### 1. 前端 DSL 不等于 LangGraph

LangGraph 不认识你的业务节点定义，编译和校验层是平台自己补的。

### 2. 节点不一定非要继承 `Runnable`

LangGraph 要的是可执行单元，类节点只是项目为了统一抽象做的选择，最常用的函数节点。

### 3. 条件汇聚和并行汇聚不是一回事

条件分支后只有一条路径会命中，真正的 fan-in 则要等待多个上游都完成。

### 4. LangGraph 不是整个运行时

调试回放、发布门禁、事件流、中断控制都还是平台层自己补齐的。

## 如果你要顺着学

建议按这个顺序看：

- 先看 [平台的可视化工作流实现](./visual-workflow.md)，理解 Workflow 这条链怎么从 DSL 变成执行图
- 再看 [平台的核心 Agent 层](./core-agent-layer.md)，理解同一套编排思路怎么落到对话执行层
- 最后回看这一篇，你会更容易把 `StateGraph`、reducer、条件路由和消息状态这些概念放回工程语境里

## 我现在的判断

这个项目把 LangGraph 放在了两个最合适的位置上：

- 在 Workflow 里，做“配置图 -> 可执行图”的编排内核
- 在 Agent 里，做“消息状态 + 工具循环 + 条件退出”的执行骨架

只靠 LangGraph 本身，平台还不完整；但没有 LangGraph，这两条链路会很快退化成难维护的条件判断和状态拼装代码。
