---
sidebar_position: 1
---

#  一站式 Agent 开发平台拆解

这里更关心一件事：怎样把这个一站式 AI Agent 开发平台先做出来，再把里面的能力拆成可以真正实现、验证和维护的工程模块。

这一栏不是泛泛复述某个现成产品，而是严格按这个项目的真实链路去拆：多模型接入、Agent 对话、RAG、可视化工作流、MCP 工具、语音交互、多端发布、模板能力和治理能力。

## 这一栏现在优先放什么

- 按项目描述逐个拆：Agent 执行层、RAG、工作流、工具平台、多模型治理、记忆机制、部署发布
- 先把 LangChain 组件层和 LangGraph 编排层讲清，再往下展开 Agent、RAG、Workflow 这些平台能力
- 把 LangGraph、StateGraph、MCP、RAG、Weaviate、Faiss、Celery、Flask、Vue 这些能力放回工程位置
- 记录 draft_graph / graph、双模式 Agent、摘要记忆、Tool 化检索、统一工具抽象这些核心实现
- 功能从概念到实现时的边界、代价和系统问题

## 先看什么

- [一站式 AI Agent 开发平台项目总览](./one-stop-agent-platform-overview.md)：先把项目目标、能力范围、技术栈和核心链路讲清楚
- [平台的 LangChain 组件抽象](./langchain-component-abstractions.md)：先把模型、Prompt、工具、消息和 RAG 文本链这些标准零件放回工程位置
- [平台的 LangGraph 编排骨架](./langgraph-orchestration-kernel.md)：再看 Workflow 和 Agent 两条执行链为什么都建立在 StateGraph 之上
- [平台的核心 Agent 层](./core-agent-layer.md)：先拆 LangGraph 执行链路、事件流、会话中断和记忆机制
- [平台的工具调用设计](./tool-calling.md)：再看 Builtin Tool / API Tool / MCP Tool 的统一工具体系
- [平台的在线知识库设计](./online-knowledge-base.md)：把上传、解析、切分、检索、融合、Tool 化复用，以及短期 / 长期记忆注入拆开
- [平台的可视化工作流实现](./visual-workflow.md)：把前端 DSL、`draft_graph / graph`、LangGraph 编译、条件分支与并行汇聚、以及工作流工具化复用讲清楚
- [平台的多模型集成实现](./multi-model-integration.md)：把 Provider-Model 两级结构、YAML 注册中心、能力标记、API Key 隔离和运行时装配拆开
- [平台的整体架构构建](./overall-architecture.md)：从模块、数据流、运行时、部署与治理层面搭起整个平台
- [怎么把一篇Agent平台文档写得真正有用](./how-to-write-useful-dify-app-note.md)：写法标准，后面每篇正文都按它来校验

## 这里不放什么

- 纯粹的产品说明书
- 只讲概念、不落结构的泛泛笔记
- 只是记录“我试过什么”的流水账

## 这一栏的输出标准

- 不只说实现了什么能力，还要说清为什么这么拆
- 不只看效果，还要拆出它背后的模块边界和数据流
- 不只写原型，还要写清工程代价、维护成本和可扩展性

## 我为什么单独开这一栏

因为这个项目真正难的，不是“有多少页面”，而是把 Agent、RAG、工作流、MCP、模型治理、语音、多端发布和部署这整条链路做成一个能跑、能扩、能管的平台。

我想把这些问题单独留一块地方，后面持续补进去，并且尽量写成能直接复用到下一个 Agent 项目的工程笔记。
