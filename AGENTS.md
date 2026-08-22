# Project Instructions

These instructions apply to the entire `my-dify` repository.

## 代码注释规范

- 新增或修改的代码注释、模块说明和方法文档字符串默认使用简短中文。
- DeepAgents、LangChain、Backend、Skill 等必要技术名词可保留英文。
- 方法文档字符串只说明职责、关键输入输出或非显然约束，优先控制在一至三行。
- 注释重点解释设计原因和边界，不复述代码，不记录迁移历史、阶段编号或版本流水账。
- 长篇背景和方案说明写入 `docs/`，代码旁只保留理解当前实现所必需的信息。
- 删除无信息量的编号步骤注释；复杂分支仅在原因不直观时添加就近短注释。

## DeepAgents V2 hard constraint: framework-native first

For every Agent, memory, context, filesystem, Skills, multi-agent, HITL, and
persistence change, use this decision order:

1. Use DeepAgents native parameters, middleware, backends, memory, and tools.
2. If DeepAgents does not cover the requirement, use LangChain or LangGraph
   middleware, callbacks, checkpoints, stores, interrupts, and state APIs.
3. If the framework contract does not match my-dify business data, implement
   only the smallest protocol adapter, permission boundary, or persistence
   projection required to connect them.
4. Add a custom component only when the framework capability is genuinely
   missing. Record the missing capability, evaluated native alternatives, why
   they fail, and the automated acceptance test before implementation.

Do not wrap, fork, or duplicate a framework component merely to rename it or
add logging. In particular, do not maintain a second implementation of:

- conversation summarization, token trimming, or context-window calculation;
- checkpoint storage or saver lifecycle;
- filesystem, Skills, Todo, SubAgent, AsyncSubAgent, or HITL middleware;
- vector infrastructure when the project already uses Weaviate.

Allowed custom boundaries are limited to:

- my-dify database models, CRUD, versioning, and audit rules;
- account/app/end-user ownership and tenant isolation;
- adapters that expose business data through native DeepAgents/LangGraph
  Backend, Memory, Skills, Store, or state contracts;
- the existing QueueEvent/SSE translation boundary;
- business-specific orchestration state that the frameworks do not provide.

For memory specifically:

- use LangGraph checkpoints for short-term thread state;
- use DeepAgents `SummarizationMiddleware` for compaction and history offload;
- use DeepAgents `memory=` plus a Backend/Store for long-term memory projection;
- keep PostgreSQL business facts authoritative and user-manageable;
- treat any Weaviate memory index as optional, rebuildable search projection;
- never add pgvector or a custom cosine-search path alongside Weaviate.

The decision source is `docs/deepagents-v2-plan/`. Update those documents and
the active `.codex-tasks/` acceptance artifacts whenever this boundary changes.
