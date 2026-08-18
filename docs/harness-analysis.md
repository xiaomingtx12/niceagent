# My Dify — Harness 视角架构拆解

> 以 AI Harness（编排控制平面）视角拆解 My Dify 平台的控制流、编排模式、工具集成、状态管理和可观测性设计。

---

## 0. 什么是 Harness 视角

AI Harness 是 AI 系统的**编排控制平面**——它不直接执行推理，而是决定：

| 维度 | 关注点 |
|------|--------|
| **路由** | 请求如何分发到正确的执行引擎 |
| **编排** | 多个 AI 组件如何按序/并行协作 |
| **工具调度** | 外部能力如何注册、发现、调用 |
| **状态管理** | 对话历史、中间结果、长期记忆如何流转 |
| **可观测性** | 推理链路如何追踪、流式输出如何投递 |
| **安全边界** | 输入过滤、输出审核、沙箱执行如何落地 |

My Dify 作为一个类 Dify 的 AI 应用平台，其 Harness 层贯穿整个 `internal/` 目录。以下按自顶向下的请求流进行拆解。

---

## 1. 请求入口与路由分发

```
客户端 (Web/OpenAPI/微信)
  │
  ▼
┌─────────────────────────────────────────────────────┐
│  app/http/app.py — Flask 应用工厂                    │
│    ├─ module.py: python-injector DI 绑定             │
│    ├─ Middleware: JWT + API Key 双模鉴权              │
│    └─ RateLimit: Redis 令牌桶限流                    │
└───────────────────────┬─────────────────────────────┘
                        │
  ┌─────────────────────▼───────────────────────────┐
  │  internal/router/router.py                       │
  │    ├─ Blueprint: my_dify (JWT 鉴权, Web 端)      │
  │    ├─ Blueprint: openapi  (API Key 鉴权, 开放端) │
  │    └─ 25+ Handler 路由注册                        │
  └─────────────────────┬───────────────────────────┘
                        │
  ┌─────────────────────▼───────────────────────────┐
  │  internal/handler/*.py — 薄控制器层              │
  │    ├─ @login_required  身份校验                   │
  │    ├─ @rate_limit      频率控制                   │
  │    └─ Schema 校验 → 委托 Service                 │
  └─────────────────────────────────────────────────┘
```

**Harness 特征：双通道鉴权**

| 通道 | 认证方式 | 用途 |
|------|---------|------|
| `my_dify` 蓝图 | JWT Bearer Token | Web 应用端、调试器 |
| `openapi` 蓝图 | API Key | 第三方集成、开放接口 |

两种认证统一在 `internal/middleware/middleware.py` 的 `request_loader` 中解析，对 Handler 完全透明。Harness 层只关心"谁在调用"，不关心"怎么进来的"。

---

## 2. 执行引擎：双轨并行

My Dify 的 Harness 层管控两条核心执行轨道：

### 2.1 Agent 轨道（自主推理循环）

```
AssistantAgentService / AIService
  │
  ▼
┌──────────────────────────────────────────────────────────┐
│  BaseAgent (Template Method)                             │
│    │                                                     │
│    ├─ FunctionCallAgent   原生 tool_call 能力的模型       │
│    │    LangGraph: preset_op → memory_recall → llm ↔ tools│
│    │                                                     │
│    └─ ReACTAgent          无 tool_call 能力的模型         │
│         LangGraph: preset_op → memory_recall → llm ↔ tools│
│         (通过 prompt 工程 + JSON 解析模拟 tool_call)       │
│                                                          │
│  执行模型：                                               │
│    stream() → 启动 daemon Thread → AgentQueueManager      │
│    invoke() → 消费 stream → 聚合 AgentResult              │
└──────────────────────────────────────────────────────────┘
```

**Harness 关键决策点：**

| 决策 | 实现位置 | 逻辑 |
|------|---------|------|
| Agent 类型选择 | `AgentConfig.agent_type` | `function_call` vs `react`，按模型能力标记路由 |
| 工具绑定 | `FunctionCallAgent._llm_node` | 检查 `ModelFeature.TOOL_CALL`，决定是否 bind_tools |
| 循环终止 | `_tools_condition` 边 | tool_calls 存在 → 回到 llm；纯文本 → END |
| 输入审核 | `preset_operation` 节点 | 关键词过滤，拦截敏感输入 |

**流式输出机制：** Agent 在 Thread 中执行 LangGraph，通过 `AgentQueueManager`（基于 `queue.Queue` 的生产者-消费者模式）将 `AgentThought` 事件推送到 Handler 层，Handler 将其包装为 SSE 流式响应。

### 2.2 Workflow 轨道（DAG 编排执行）

```
WorkflowService.debug_workflow()
  │
  ▼
┌──────────────────────────────────────────────────────────────┐
│  Workflow (extends LangChain BaseTool)                       │
│    │                                                         │
│    ├─ 构建阶段：从 WorkflowConfig 动态生成 LangGraph StateGraph│
│    │    ├─ NodeClasses 注册表（9 种节点类型 → 对应 Node 类）    │
│    │    ├─ 普通边：节点 A → 节点 B                             │
│    │    ├─ 条件边：意图分类节点 → 根据 intent_condition 路由    │
│    │    └─ 扇入边：多节点汇聚到同一目标                        │
│    │                                                         │
│    └─ 执行阶段：graph.invoke(state)                          │
│         ├─ Start → LLM/Tool/Code/IntentClassifier → ... → End│
│         └─ 每个节点返回 NodeResult(inputs, outputs, latency) │
└──────────────────────────────────────────────────────────────┘
```

**9 种节点类型及 Harness 职责：**

| 节点 | 文件 | Harness 调度职责 |
|------|------|-----------------|
| **Start** | `start_node.py` | 注入用户输入变量 |
| **LLM** | `llm_node.py` | Jinja2 模板渲染 → 模型选择 → 流式调用 → 输出提取 |
| **Tool** | `tool_node.py` | 统一调度 builtin/api/mcp 三类工具 |
| **Code** | `code_node.py` | AST 本地校验 → 腾讯云 SCF 远程沙箱执行 |
| **IntentClassifier** | `intent_classifier_node.py` | LLM 结构化输出分类 → 设置路由条件 |
| **HTTPRequest** | `http_request_node.py` | 外部 HTTP 调用 |
| **TemplateTransform** | `template_transform_node.py` | Jinja2 模板渲染 |
| **DatasetRetrieval** | `dataset_retrieval_node.py` | 知识库检索注入 |
| **End** | `end_node.py` | 收集最终输出 |

**Workflow-as-Tool 复合模式：** `Workflow` 继承 `BaseTool`，可以被 Agent 作为工具调用。这实现了 Harness 层的递归编排——Agent 调用 Workflow，Workflow 内部又可以调用包含 LLM 的节点。

---

## 3. 工具调度系统：三层统一接口

```
┌─────────────────────────────────────────────────────────┐
│                 Tool 调度层（Harness 核心）               │
│                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │ 内置工具      │  │ API 工具      │  │ MCP 工具      │  │
│  │ ──────────   │  │ ──────────   │  │ ──────────   │  │
│  │ providers.yaml│  │ DB 记录       │  │ DB 记录       │  │
│  │ ↓            │  │ ↓            │  │ ↓            │  │
│  │ BuiltinProvider│ │ ApiProvider  │  │ MCPRuntime   │  │
│  │ Manager      │  │ Manager      │  │ Manager      │  │
│  │ ↓            │  │ ↓            │  │ ↓            │  │
│  │ BaseTool     │  │ StructuredTool│  │ BaseTool     │  │
│  └──────────────┘  └──────────────┘  └──────────────┘  │
│         │                 │                 │            │
│         └─────────────────┼─────────────────┘            │
│                           ▼                              │
│                  统一 LangChain BaseTool 接口             │
│                  → Agent / Workflow 节点均可调用           │
└─────────────────────────────────────────────────────────┘
```

**三层工具的 Harness 差异：**

| 层 | 注册方式 | 运行时 | 安全策略 |
|----|---------|--------|---------|
| 内置工具 | `providers.yaml` 静态声明 | 本地 Python 调用 | 代码内建 |
| API 工具 | DB 动态注册 + OpenAPI Schema | HTTP 请求（路径/查询/头/体参数路由） | API Key / OAuth |
| MCP 工具 | DB 动态注册 + 远程发现 | `MultiServerMCPClient` 异步调用 | `MCPSecretCipher` Fernet 加密 |

**MCP 工具生命周期管理：**

```
创建/更新 MCPServer 记录
  │
  ├─ MCPSecretCipher 加密敏感字段（env_vars, headers）
  │    └─ Fernet 对称加密，密钥派生自 MCP_SECRET_KEY
  │
  ├─ MCPRuntimeManager 连接远程 MCP Server
  │    ├─ STDIO 模式：启动子进程
  │    └─ HTTP 模式：连接远程端点
  │
  ├─ 自动发现工具列表 → 持久化 MCPTool 记录
  │
  └─ 缓存策略：server_id:updated_at 作为缓存键
       └─ 配置变更自动失效
```

---

## 4. 模型抽象层：Provider 模式

```
┌────────────────────────────────────────────────────────────┐
│  LanguageModelManager (Singleton)                          │
│    ├─ 加载 providers.yaml 定义                              │
│    ├─ provider_name + model_name → Model Class             │
│    └─ 返回 LangChain ChatModel 实例                        │
│                                                            │
│  支持的 Provider：                                          │
│  ┌──────────┬──────────┬──────────┬──────────┐            │
│  │ 通义千问  │ 智谱 GLM │ DeepSeek │ OpenAI   │            │
│  │ tongyi   │ zhipu    │ deepseek │ openai   │            │
│  ├──────────┼──────────┼──────────┼──────────┤            │
│  │ 百度文心  │ MiniMax  │ Moonshot │ 豆包     │            │
│  │ wenxin   │ minimax  │ moonshot │ doubao   │            │
│  └──────────┴──────────┴──────────┴──────────┘            │
│                                                            │
│  每个 Provider 实现：                                       │
│    ├─ Chat 子类（对话模型）                                  │
│    └─ Completion 子类（补全模型，仅 OpenAI）                 │
└────────────────────────────────────────────────────────────┘
```

**Harness 关键抽象：** 模型选择逻辑通过 `ModelFeature` 标记驱动。例如 `FunctionCallAgent._llm_node` 检查 `ModelFeature.TOOL_CALL` 来决定是否绑定工具——这让 Harness 层能根据模型能力自适应执行策略。

---

## 5. 知识检索：RAG 编排

```
用户查询
  │
  ▼
┌─────────────────────────────────────────────────────────┐
│  RetrievalService                                        │
│    ├─ 混合检索策略（Ensemble）                            │
│    │    ├─ SemanticRetriever (Weaviate 向量相似度)        │
│    │    ├─ FullTextRetriever (jieba 分词 + keyword_table)│
│    │    └─ 权重融合排序                                   │
│    │                                                     │
│    └─ 结果注入工作流                                      │
│         └─ DatasetRetrieval 节点标记 is_knowledge=true   │
│            → LLM 节点自动注入 reference_info 变量         │
└─────────────────────────────────────────────────────────┘
```

**Harness 模式：** 知识检索结果不是直接暴露给用户，而是作为工作流 LLM 节点的上下文注入。LLM 节点检测输入中的 `is_knowledge=true` 标记，自动将其包装为 `reference_info` 变量供 Jinja2 模板引用。Harness 层控制了知识的"消费方式"。

---

## 6. 记忆与状态管理

```
┌────────────────────────────────────────────────────────┐
│  短期记忆：TokenBufferMemory                            │
│    ├─ 从 DB 按时间倒序拉取最近 N 条消息                  │
│    ├─ tiktoken 计数 → trim_messages 截断                │
│    └─ 保留 human/AI 消息配对完整性                       │
│                                                        │
│  长期记忆（Agent 可选）：                                │
│    ├─ AgentConfig.enable_long_term_memory 开关          │
│    ├─ 记忆召回节点在 Agent 循环前执行                     │
│    └─ 历史摘要注入 system prompt                        │
│                                                        │
│  工作流状态：WorkflowState                               │
│    ├─ nodes: Dict[str, NodeResult] 节点输出映射          │
│    ├─ inputs: Dict 原始用户输入                          │
│    └─ 每个节点可引用上游节点的 outputs                   │
└────────────────────────────────────────────────────────┘
```

**状态流转模式：**
- Agent 轨道：`AgentState` 继承 LangGraph `MessagesState`，附加 `iteration_count`、`history`、`long_term_memory` 字段
- Workflow 轨道：`WorkflowState` 以字典形式在节点间传递，每个节点读取上游输出、写入自身输出

---

## 7. 流式输出与可观测性

```
Agent/Workflow 执行
  │
  ├─ Agent 轨道：AgentQueueManager (queue.Queue)
  │    ├─ AgentThought 事件类型：thinking / tool_call / tool_result / answer
  │    ├─ daemon Thread 生产 → Handler 消费
  │    └─ SSE 事件流推送
  │
  └─ Workflow 轨道：节点级 SSE 事件
       ├─ 每个节点执行完推送 NodeResult
       ├─ 包含 inputs / outputs / latency
       └─ WorkflowService.debug_workflow() 组装事件流
```

**可观测性覆盖：**

| 维度 | 覆盖情况 |
|------|---------|
| 推理链路 | AgentThought 事件流（thinking/tool_call/tool_result/answer） |
| 节点级延迟 | 每个 Workflow 节点记录 latency |
| 工具调用 | tool_call + tool_result 成对事件 |
| 对话分析 | AnalysisService 提供会话/消息统计 |
| 结果持久化 | workflow_results 表记录调试结果 |

---

## 8. 安全边界

```
┌─────────────────────────────────────────────────────────┐
│  输入层                                                  │
│    ├─ Middleware: JWT/API Key 身份验证                    │
│    ├─ RateLimit: Redis 令牌桶频率控制                     │
│    └─ Agent preset_operation: 关键词内容过滤              │
│                                                         │
│  执行层                                                  │
│    ├─ Code 节点: AST 格式校验 + 腾讯云 SCF 沙箱执行      │
│    ├─ MCP 工具: MCPSecretCipher Fernet 加密敏感配置       │
│    └─ API 工具: 参数路由隔离（path/query/header/body）    │
│                                                         │
│  输出层                                                  │
│    └─ 待增强：当前缺乏结构化输出审核 / Guard 链           │
└─────────────────────────────────────────────────────────┘
```

---

## 9. 辅助 Agent（元编排层）

`AssistantAgentService` 是一个特殊的 Harness 组件——它本身是一个 Agent，但其工具是"创建其他 AI 应用"的能力：

```
用户："帮我创建一个翻译工作流"
  │
  ▼
AssistantAgent (FunctionCallAgent)
  ├─ Tool: create_app          → Celery 异步创建应用
  ├─ Tool: workflow_generator   → LLM 生成工作流 DAG 配置
  ├─ Tool: app_with_workflow    → 一键创建应用+工作流
  └─ Tool: knowledge_retrieval  → FAISS 检索内置知识库
       └─ 回答平台使用相关问题
```

**Harness 意义：** 这是一个"编排的编排"——用 Agent 来编排平台资源的创建过程。Agent 的工具不是外部 API，而是平台自身的 Service 层能力。

---

## 10. 整体 Harness 架构图

```
                          ┌─────────────────────────────┐
                          │         客户端               │
                          │  Web / OpenAPI / 微信        │
                          └──────────────┬──────────────┘
                                         │
              ┌──────────────────────────▼──────────────────────────┐
              │              Harness 控制平面                        │
              │                                                     │
              │  ┌─────────────────────────────────────────────┐   │
              │  │  路由层：JWT/API Key 鉴权 + 频率限制         │   │
              │  └─────────────────────┬───────────────────────┘   │
              │                       │                             │
              │  ┌────────────────────▼────────────────────────┐   │
              │  │  编排层                                      │   │
              │  │  ├─ Agent 引擎（自主推理循环）                │   │
              │  │  │   └─ FunctionCall / ReACT 双模            │   │
              │  │  ├─ Workflow 引擎（DAG 编排）                 │   │
              │  │  │   └─ 9 种节点 + 条件路由                   │   │
              │  │  └─ Assistant Agent（元编排）                 │   │
              │  │      └─ 用 Agent 编排平台资源创建             │   │
              │  └────────────────────┬────────────────────────┘   │
              │                       │                             │
              │  ┌────────────────────▼────────────────────────┐   │
              │  │  调度层                                      │   │
              │  │  ├─ 语言模型调度（8 家 Provider 统一接口）    │   │
              │  │  ├─ 工具调度（内置/API/MCP 三层统一接口）    │   │
              │  │  ├─ 知识检索调度（语义/全文/混合）            │   │
              │  │  └─ 记忆管理（短期截断 + 长期摘要）           │   │
              │  └────────────────────┬────────────────────────┘   │
              │                       │                             │
              │  ┌────────────────────▼────────────────────────┐   │
              │  │  安全层                                      │   │
              │  │  ├─ 输入关键词过滤                           │   │
              │  │  ├─ 代码沙箱执行（腾讯云 SCF）               │   │
              │  │  ├─ MCP 敏感配置加密（Fernet）               │   │
              │  │  └─ API 参数路由隔离                         │   │
              │  └────────────────────┬────────────────────────┘   │
              │                       │                             │
              │  ┌────────────────────▼────────────────────────┐   │
              │  │  可观测层                                    │   │
              │  │  ├─ AgentThought SSE 事件流                  │   │
              │  │  ├─ 节点级延迟追踪                           │   │
              │  │  ├─ 工具调用记录                             │   │
              │  │  └─ 会话/消息分析统计                        │   │
              │  └─────────────────────────────────────────────┘   │
              └─────────────────────────────────────────────────────┘
                                         │
              ┌──────────────────────────▼──────────────────────────┐
              │              数据平面                                 │
              │  PostgreSQL │ Redis │ Weaviate │ FAISS │ Celery     │
              │  腾讯云 COS │ SCF   │ MCP Servers                    │
              └─────────────────────────────────────────────────────┘
```

---

## 11. Harness 设计评价

### 优势

| 设计点 | 评价 |
|--------|------|
| **Workflow-as-Tool** | 工作流可被 Agent 当工具调用，实现递归编排，扩展性强 |
| **统一工具接口** | 三层工具都收敛到 LangChain BaseTool，对上层透明 |
| **双模 Agent** | FunctionCall / ReACT 按模型能力自适应，兼容性强 |
| **Provider 抽象** | 8 家模型供应商统一接入，新增供应商只需实现 Chat 子类 |
| **MCP 集成** | 支持 STDIO/HTTP 双传输模式，工具发现自动化 |
| **流式输出** | 生产者-消费者队列模式，支持实时推理流推送 |

### 待改进

| 问题 | 影响 | 建议方向 |
|------|------|---------|
| **工作流串行执行** | 无并发节点能力，多分支工作流吞吐量受限 | LangGraph 原生支持并行分支，可利用 fan-out/fan-in |
| **记忆策略单一** | TokenBufferMemory 仅做截断，长对话丢失上下文 | 引入分层记忆（短期窗口 + 中期摘要 + 长期检索） |
| **缺乏 Guard 链** | 安全过滤仅关键词，无结构化审核管线 | 引入 Guard Rail 框架（输入审核 → 输出审核 → 事实核查） |
| **错误恢复有限** | 节点失败无重试/降级策略 | 增加节点级重试、断路器、降级路径 |
| **缺乏 Trace ID** | 跨服务调用无统一追踪标识 | 引入 OpenTelemetry 或自定义 trace_id 贯穿全链路 |
| **缓存策略粗粒度** | MCP 缓存仅按 server_id:updated_at，工具级无缓存 | 增加工具调用结果缓存（幂等操作） |

---

## 12. 总结

My Dify 的 Harness 层采用**分层编排**架构：

1. **路由层**统一入口，双通道鉴权
2. **编排层**提供 Agent（自主推理）、Workflow（DAG 编排）、Assistant Agent（元编排）三种执行模式
3. **调度层**将模型、工具、检索、记忆统一为可插拔的抽象接口
4. **安全层**在输入、执行、配置三个维度建立边界
5. **可观测层**通过事件流实现推理链路透明化

核心设计理念：**Harness 控制"怎么组合"，组件负责"怎么执行"**。Agent、Workflow、Tool、Model 都是独立的可组合单元，Harness 层通过 LangGraph StateGraph 将它们编织为完整的执行链路。
