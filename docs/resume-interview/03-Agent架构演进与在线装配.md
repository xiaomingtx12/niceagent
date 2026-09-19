# Agent 架构演进与在线装配：面试准备

## 简历主张与编写思路

对应成稿第一条。用「自建 LangGraph Agent → DeepAgents 运行时」交代演进，接着解释在线配置如何转换为可执行能力，最后体现权限与调用治理。不要将框架名称切换本身当作技术收益。

## 一分钟口述

这个平台允许用户在线选择模型、提示词、工具、Skills、知识库、工作流和子 Agent。后端先解析草稿或发布定义，校验资源归属和模型连接，再把已授权能力交给 Core 装配。项目从自建 LangGraph Agent 演进到 DeepAgents，复用框架的规划、文件工具、SubAgent 和摘要能力，平台继续负责身份、配置、任务、事件和资源生命周期。主子 Agent 不直接共享全部授权，自定义子 Agent 显式绑定工具与 Skill 范围。工具调用还经过重名校验、消息校验、有限重试和次数限制。

## 从配置到执行器

1. 入口解析可信账号、应用、会话和调用来源；浏览器参数不能直接决定资源归属。
2. AgentDefinitionService 选择对应的应用定义；AgentFactory 解析模型、工具、Skills、子 Agent 和输入材料。
3. API/MCP 工具适配为框架工具，知识库和工作流也以工具形式提供能力。
4. AgentRuntimeAssembler 创建工作副本、Skill 投影，取得共享 Store、Checkpointer，装配沙箱和文件路由。
5. build_deep_agent 构建 system、中间件、主子 Agent 规格，通过 create_deep_agent 编译图。
6. 返回单次使用的 AgentExecutor。一次执行完成或等待输入后释放本轮独占资源；恢复时重新装配执行器。

## 框架与平台各自负责什么

| 层次 | 职责 |
| --- | --- |
| DeepAgents/LangGraph | 模型工具循环、图执行、规划和子 Agent 原语、文件工具、摘要、Checkpoint 与 interrupt。 |
| 平台 Service | 应用/资源授权、草稿发布来源、模型连接、业务执行记录、并发幂等与结果事务。 |
| 平台 Core 适配 | 配置到框架规格的转换、Backend 组合、中间件策略、事件规范化和资源管理。 |

重构以后仍然依赖 LangGraph。不要说「完全替换了 LangGraph」；重点是将通用 Agent 能力交给 DeepAgents，明确平台保留的业务边界。

## 主子 Agent 的边界

同步子 Agent 被转换成原生 SubAgent 规格，配置模型、system、工具与文件权限。异步子 Agent 使用 graph_id、URL 和认证头适配 Agent Protocol，再交给异步子任务中间件。

自定义同步子 Agent 显式提供工具列表，包括空列表，避免框架默认继承父 Agent 全部工具。Skill 路由按授权配置，文件权限只开放所需范围；共享 Backend 的子 Agent 通过 ShellExecutionMiddleware 限制共享 Shell，文件路径规则本身不能约束 execute 中任意命令。

异步派发成功不等于子任务完成。当前平台展示子任务状态和结果，不应声称已实现「任一子任务结束都会自动唤醒父 Agent 并汇总」的全自动调度保证。

## 典型追问与回答要点

**为什么做这次重构？**

从可观察的职责变化说明：接入规划、文件、Skills、子 Agent、压缩等共同能力时，采用 DeepAgents 原生扩展点，减少平台重复维护通用逻辑；保留业务服务和协议边界。历史工期、删减代码量及收益未在本轮核实，面试前结合自己的真实记录准备。

**为什么所有工具都不能统一自动重试？**

外部写入可能已成功，只是响应超时。重试可能再次发送、再次扣款或写入。当前模型瞬时错误有限重试，工具重试仅面向 ls/read_file/glob/grep 等只读操作的特定错误，不能描述为任意业务工具自动容错。

**怎样避免工具装配后不可调用？**

装配时校验重名和框架保留名称；MCP 将远端 Schema 转换为工具参数结构。配置授权和模型连接先在平台解析，Core 不自行查业务权限。

**在线修改配置是否立即改变正在执行的图？**

一轮执行使用装配时生成的定义。后续新执行重新解析配置；等待输入的恢复还会核对配置指纹，不能直接用已变化的定义接着跑。在线配置不等于运行中任意热替换模型和工具。

**MCP 和 Skill 有什么区别？**

MCP 提供外部服务的工具协议及调用接口。Skill 提供任务说明、资源和可能的脚本，按授权作为文件能力提供。读取 Skill 不必启动远程沙箱，执行脚本才会触发对应执行环境。

## 场景推演

主 Agent 可查询资料、生成报告；子 Agent 仅被授权某个检索工具和格式规范 Skill。装配时应显式绑定子 Agent 的工具和 Skill 路径，不能因为主 Agent 能访问长期记忆或 Shell，就让子 Agent自动获得相同能力。子 Agent 生成临时文件后，主 Agent 负责显式交付。

## 代码入口

- [AgentFactory](../../internal/service/agent/agent_factory.py)：平台配置、模型和能力解析。
- [运行资源装配](../../internal/core/agent_v2/assembly/runtime_assembler.py)：build、_build_runtime。
- [DeepAgents 构图](../../internal/core/agent_v2/assembly/graph/deep_agent_factory.py)：build_deep_agent。
- [子 Agent 规格](../../internal/core/agent_v2/assembly/graph/subagent_spec_adapter.py)：build_subagent_specs、validate_agent_tools。
- [中间件策略](../../internal/core/agent_v2/assembly/graph/platform_middleware.py)：重试与调用限制。
- [MCP 适配](../../internal/core/mcp/runtime/mcp_runtime_manager.py)：发现、目录和调用。

## 面试前准备

准备一个主子 Agent 授权不同的配置、一次工具重名或越权配置被拒绝的演示，以及一张装配阶段耗时日志。说明哪些查询在本轮复用、哪些模型实例仍重新创建；没有基准数据时不报迁移提速百分比。
