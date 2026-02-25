# Bug Report - 2025-02-25

## Bug 14：Celery 长耗时任务中数据库与 Redis 连接因空闲超时断开

**现象**：通过辅助 Agent 的"创建 Agent 应用 + 工作流"功能触发 Celery 任务后，任务执行失败，Celery 日志报两个错误：

1. `psycopg2.OperationalError: server closed the connection unexpectedly`
2. `redis.exceptions.ConnectionError: Error while reading from 106.54.40.123:6379 : (10054, '远程主机强迫关闭了一个现有的连接。')`

**根因**：`auto_generate_app_with_workflow` 任务的执行流程是：查询 account → 调用 LLM 生成工作流配置 → 写入数据库。其中 LLM 调用耗时 30-60 秒，在这段时间内：

- PostgreSQL 连接完全空闲，被远程数据库服务器或中间网络层（防火墙/NAT）主动关闭
- Redis 连接同样因空闲被远程 Redis 服务器断开

等 LLM 返回后，任务尝试用已断开的数据库连接查询 `account` 表，触发 `OperationalError`。随后 Celery 尝试通过 Redis 上报任务结果，又触发 `ConnectionError`。

**修复**：两个层面的改动。

### 修复 1：SQLAlchemy 连接池启用 `pool_pre_ping`

在 `config/config.py` 的 `SQLALCHEMY_ENGINE_OPTIONS` 中增加 `pool_pre_ping: True`。该选项使 SQLAlchemy 在每次从连接池取出连接时，先发送一个轻量级的 ping 检测连接是否存活。如果连接已断开，自动丢弃并创建新连接。

```python
# config/config.py
self.SQLALCHEMY_ENGINE_OPTIONS = {
    "pool_size": int(_get_env("SQLALCHEMY_POOL_SIZE")),
    "pool_recycle": int(_get_env("SQLALCHEMY_POOL_RECYCLE")),
    "pool_pre_ping": True,  # 新增：取连接前检测存活性
}
```

### 修复 2：Celery 任务中拆分 LLM 调用与数据库操作

重构 `workflow_task.py` 的 `auto_generate_app_with_workflow` 任务，将 LLM 调用（耗时长、不需要数据库）和数据库写入（需要活跃连接）分开执行：

```python
# 1. 先查询 account 验证存在性
account = db.session.query(Account).get(UUID(account_id))

# 2. 调用 LLM 生成配置（耗时 30-60 秒，期间数据库连接可能断开）
workflow_config = workflow_generator_service.generate_workflow_config_with_llm(...)

# 3. LLM 调用完成后，关闭旧 session 释放可能已断开的连接
db.session.remove()

# 4. 重新获取 account（使用新鲜的数据库连接，pool_pre_ping 会自动检测重连）
account = db.session.query(Account).get(UUID(account_id))

# 5. 执行数据库写入操作
validated_config = workflow_generator_service._validate_and_fix_workflow_config(...)
workflow = workflow_generator_service.create(Workflow, ...)
```

同时在两个任务的 `finally` 块中添加 `db.session.remove()` 确保连接归还到连接池。

**两个修复配合工作**：即使 LLM 调用期间连接被远程服务器断开，`session.remove()` 释放旧连接，`pool_pre_ping` 在下次取连接时自动检测并重连。

**涉及文件**：
- `my-dify/config/config.py` — 增加 `pool_pre_ping: True`
- `my-dify/internal/task/workflow_task.py` — 重构任务流程，拆分 LLM 调用与数据库操作，添加 `session.remove()`


---

## Bug 15：会话摘要/建议问题/会话命名在 model_config 为 None 时崩溃

**现象**：辅助 Agent 对话后，控制台报两个错误：

1. `_generate_summary_and_update` 中：`TypeError: Expected a Runnable, callable or dict. Instead got an unsupported type: <class 'NoneType'>`
2. `generate_suggested_questions` 中：`AttributeError: 'NoneType' object has no attribute 'with_structured_output'`

**根因**：`conversation_service.py` 中 `summary`、`generate_conversation_name`、`generate_suggested_questions` 三个方法的模型加载逻辑存在缺陷：

```python
llm = None
try:
    if model_config and account_id:
        llm = language_model_service.load_language_model(model_config, account_id)
except Exception as e:
    logging.warning(f"加载配置模型失败: {str(e)}")

# llm 仍然是 None，直接用于构建链 → 崩溃
summary_chain = prompt | llm | StrOutputParser()
```

当 `model_config` 为 `None` 时（`if model_config and account_id` 为 `False`），`llm` 保持 `None`，后续 `prompt | llm` 触发 LangChain 的类型检查错误。

触发路径：`assistant_agent_service.py` 调用 `save_agent_thoughts` 时传入的 `app_config` 为 `{"long_term_memory": {"enable": True}}`，不包含 `model_config` 键。`app_config.get("model_config")` 返回 `None`，传递给 `_generate_summary_and_update`。

同样，`generate_suggested_questions` 通过 `ai_service.py` 调用时，如果应用的 `draft_app_config.model_config` 为空，也会传入 `None`。

**修复**：当 `model_config` 为 `None` 时，使用默认的 tongyi 配置（`qwen-plus`），通过 `load_language_model` 加载模型，该方法内部会调用 `get_api_key_for_provider("tongyi", account_id)` 获取用户自己配置的 API Key。如果用户未配置 tongyi 的 API Key，会抛出明确的异常提示用户去配置，而不是使用平台的默认 Key。

```python
# 如果未传入 model_config，使用默认的 tongyi 配置（需要用户自己的 API Key）
if not model_config:
    model_config = {"provider": "tongyi", "model": "qwen-plus", "parameters": {}}

llm = None
try:
    if account_id:
        llm = language_model_service.load_language_model(model_config, account_id)
except Exception as e:
    logging.warning(f"加载模型失败: {str(e)}")
    return old_summary  # 或 "新的会话" 或 []，根据方法返回安全默认值

if llm is None:
    return old_summary
```

加载失败时（如用户未配置 API Key），返回安全的默认值而非崩溃：
- `summary` → 返回 `old_summary`（保持原摘要不变）
- `generate_conversation_name` → 返回 `"新的会话"`
- `generate_suggested_questions` → 返回 `[]`（空列表）

**涉及文件**：
- `my-dify/internal/service/conversation_service.py` — `summary`、`generate_conversation_name`、`generate_suggested_questions` 三个方法

---

## Bug 16：OpenAPI 服务加载模型时未传入 account_id，无法获取用户 API Key

**现象**：通过 OpenAPI 接口调用应用对话时，模型加载不会使用用户配置的 API Key，而是依赖环境变量中的 Key（平台的）或直接报错。

**根因**：`openapi_service.py` 第 102 行调用 `load_language_model` 时只传了 `model_config`，没有传 `account_id`：

```python
# 修复前
llm = self.language_model_service.load_language_model(
    app_config.get("model_config", {})
)
```

`load_language_model` 内部逻辑是 `if account_id:` 才去获取用户 API Key，不传 `account_id` 就跳过了 API Key 获取步骤。

此外，`OpenAPIService` 类没有注入 `language_model_service` 依赖（dataclass 字段中缺失），`self.language_model_service` 在运行时会触发 `AttributeError`。

**修复**：

1. 在 `OpenAPIService` 的 dataclass 字段中添加 `language_model_service: LanguageModelService`
2. 调用时传入 `account_id=account.id`

```python
# 修复后
llm = self.language_model_service.load_language_model(
    app_config.get("model_config", {}),
    account_id=account.id,
)
```

**涉及文件**：
- `my-dify/internal/service/openapi_service.py`

---

## Bug 17：微信公众号服务加载模型时未传入 account_id，无法获取用户 API Key

**现象**：微信公众号对话时，模型加载不使用应用创建者配置的 API Key。

**根因**：`wechat_service.py` 的 `_thread_chat` 方法中，`load_language_model` 调用缺少 `account_id`：

```python
# 修复前
llm = self.language_model_service.load_language_model(
    app_config.get("model_config", {})
)
```

**修复**：传入 `account_id=app.account_id`（使用应用创建者的 API Key）：

```python
# 修复后
llm = self.language_model_service.load_language_model(
    app_config.get("model_config", {}),
    account_id=app.account_id,
)
```

**涉及文件**：
- `my-dify/internal/service/wechat_service.py`

---

## Bug 18：AI 服务 optimize_prompt 在 model_config 为 None 时崩溃

**现象**：当应用没有配置 `model_config` 时，调用 Prompt 优化功能会报 `AttributeError: 'NoneType' object has no attribute 'get'`。

**根因**：`ai_service.py` 的 `optimize_prompt` 方法中，`model_config` 可能为 `None`（当应用的 `draft_app_config.model_config` 为空时），直接传给 `load_language_model(model_config, account.id)`，而 `load_language_model` 内部调用 `model_config.get("provider", "")` 会对 `None` 报错。

```python
# 修复前
model_config = None
if app:
    draft_config = app.draft_app_config
    if draft_config and draft_config.model_config:
        model_config = draft_config.model_config

llm = self.language_model_service.load_language_model(model_config, account.id)
# model_config 为 None 时 → AttributeError
```

**修复**：当 `model_config` 为 `None` 时，使用默认的 tongyi 配置（需要用户自己的 API Key）：

```python
# 修复后
if not model_config:
    model_config = {"provider": "tongyi", "model": "qwen-plus", "parameters": {}}

llm = self.language_model_service.load_language_model(model_config, account.id)
```

**涉及文件**：
- `my-dify/internal/service/ai_service.py`

---

## API Key 系统设计全面审查

对整个 API Key 系统进行了全面审查，覆盖后端模型层、服务层、辅助函数、路由处理器，以及前端组件。

### 系统架构概览

```
用户配置 API Key 的完整链路：

前端                          后端                              数据库
┌─────────────────┐    ┌──────────────────────┐    ┌─────────────────┐
│ ApiKeyRequired  │    │ ProviderApiKey       │    │ provider_api_key│
│ Modal.vue       │───>│ Handler              │───>│ 表              │
│ (首次登录提示)   │    │                      │    │                 │
├─────────────────┤    ├──────────────────────┤    │ - account_id    │
│ ProviderApiKeys │    │ ProviderApiKey       │    │ - provider_name │
│ View.vue        │───>│ Service              │    │ - api_key       │
│ (管理页面)       │    │                      │    │ - is_active     │
├─────────────────┤    ├──────────────────────┤    └─────────────────┘
│ CreateProvider  │    │ api_key_helper.py    │           │
│ ApiKeyModal.vue │    │ get_api_key_for_     │<──────────┘
│ (创建弹窗)       │    │ provider()           │
└─────────────────┘    └──────────┬───────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    │ load_language_model()      │
                    │ (LanguageModelService)     │
                    │ 内部调用 get_api_key_for_  │
                    │ provider() 获取用户 Key    │
                    └───────────────────────────┘
```

### 调用链路审查结果

| 调用方 | 文件 | 是否传入 account_id | 状态 |
|--------|------|---------------------|------|
| 应用调试对话 | `app_service.py` | ✅ `account.id` | 正常 |
| WebApp 对话 | `web_app_service.py` | ✅ `account.id` | 正常 |
| OpenAPI 对话 | `openapi_service.py` | ❌ 未传入 | Bug 16 已修复 |
| 微信公众号对话 | `wechat_service.py` | ❌ 未传入 | Bug 17 已修复 |
| 辅助 Agent 对话 | `assistant_agent_service.py` | ✅ 直接用 `api_key` 参数 | 正常 |
| Prompt 优化 | `ai_service.py` | ✅ `account.id`（但 model_config 可能为 None） | Bug 18 已修复 |
| 建议问题生成 | `ai_service.py` → `conversation_service.py` | ✅ `account.id` | 正常（Bug 15 已修复） |
| 会话摘要/命名 | `conversation_service.py` | ✅ `account_id` | 正常（Bug 15 已修复） |
| 工作流 LLM 节点 | `llm_node.py` | ✅ `account_id` | 正常 |
| 工作流意图分类节点 | `intent_classifier_node.py` | ✅ `account_id` | 正常 |
| 工作流生成 | `workflow_generator_service.py` | ✅ `account.id` | 正常 |
| 嵌入模型初始化 | `embeddings_service.py` | ✅ `account_id` | 正常 |
| 语音转文本 | `audio_service.py` | ✅ `account.id` | 正常 |
| 文本转语音 | `audio_service.py` | ✅ `account.id` | 正常 |
| 自动创建应用 | `app_service.py` | ✅ `account_id` | 正常 |

### 设计优点

1. **用户隔离**：`ProviderApiKey` 表通过 `account_id` 隔离，每个用户独立管理自己的 API Key，有联合索引 `(account_id, provider_name)` 加速查询
2. **统一入口**：`get_api_key_for_provider()` 作为唯一的 API Key 获取入口，未配置时抛出明确异常提示用户去配置
3. **安全脱敏**：API 响应中对密钥做了脱敏处理（只显示前 8 位和后 4 位），前端使用 `api_key_masked` 字段展示
4. **前端引导**：`ApiKeyRequiredModal.vue` 在用户首次登录时弹窗提示配置 API Key，区分必须配置（tongyi）和可选配置（openai 等）
5. **唯一性约束**：每个用户每个提供商只允许一个密钥配置，创建时检查 `existing` 避免重复
6. **启用/禁用控制**：`is_active` 字段支持临时禁用某个提供商的 Key，不需要删除

### 需要上线前解决的问题

#### 问题 1：API Key 明文存储（安全风险：高）

`provider_api_key` 表的 `api_key` 字段是 `String(500)` 明文存储。上线后如果数据库被入侵，所有用户的 API Key 会直接泄露。

**建议**：使用 AES-256 加密存储，读取时解密。可以在 `ProviderApiKey` 模型上添加 `@property` 做透明加解密，或在 `ProviderApiKeyService` 的 `create` 和 `get_active_api_key_by_provider` 中处理。加密密钥通过环境变量注入，不存储在代码中。

#### 问题 2：`load_default_language_model()` 方法应标记废弃或删除（安全风险：中）

该方法直接实例化 OpenAI `gpt-4o-mini` 模型，不接受 `account_id` 参数，不获取用户 API Key。上线后如果被误调用，要么使用环境变量中的平台 Key（违反用户隔离原则），要么直接报错。

当前状态：经过本次修复，已确认没有代码调用此方法。但方法仍然存在，后续开发可能误用。

**建议**：添加 `@deprecated` 标记或直接删除。

**已处理**：已删除 `load_default_language_model()` 方法。

#### 问题 3：前端 `ApiKeyRequiredModal` 的 localStorage 策略（体验风险：低）

用户点击"我知道了"后，`localStorage` 标记 `api_key_modal_shown = true`，之后永远不再提示。如果用户关闭弹窗但没有去配置 API Key，后续使用功能时才会报错。

**建议**：改为检查用户是否已配置至少一个 API Key（调用后端接口），而不是依赖 localStorage 标记。或者在关键功能入口（如创建应用、开始对话）前端主动检查 API Key 配置状态。

**已处理**：移除了 localStorage 策略，改为每次进入页面时调用 `getProviderApiKeysWithPage({ current_page: 1, page_size: 1 })` 检查用户是否已配置 API Key。如果 `total_record === 0`（未配置任何 Key），才弹窗提示。用户配置 Key 后，下次进入页面不再弹窗。

---

## Bug 19：AI 对话流式响应在前端显示空白，刷新后才能看到内容

**现象**：辅助 Agent 对话、应用调试对话、WebApp 对话、OpenAPI 对话中，后端生成完成后前端消息区域显示空白（0.00s, 0 Tokens），但"显示运行流程"面板有数据。刷新浏览器后内容正常显示。

**根因**：Python `str` 枚举在 f-string 中的行为问题。后端 SSE 事件使用 f-string 格式化 `QueueEvent` 枚举值：

```python
yield f"event: {agent_thought.event}\ndata:{json.dumps(data)}\n\n"
```

`QueueEvent` 定义为 `class QueueEvent(str, Enum)`。在 Python 3.10+ 中，`f"{QueueEvent.AGENT_MESSAGE}"` 返回的是 `"QueueEvent.AGENT_MESSAGE"`（枚举的 `__format__` 返回类名.成员名），而不是期望的 `"agent_message"`（枚举值）。

实际发送的 SSE：
```
event: QueueEvent.AGENT_MESSAGE
data:{"event":"agent_message","thought":"你好",...}
```

前端期望的 SSE：
```
event: agent_message
data:{"event":"agent_message","thought":"你好",...}
```

前端 `handleStream` 解析出 `event = "QueueEvent.AGENT_MESSAGE"`，在 `HomeView.vue` 中与 `QueueEvent.agentMessage`（值为 `"agent_message"`）比较不匹配，导致 `agent_message` 事件被跳过。`answer` 始终为空，`latency` 和 `total_token_count` 保持初始值 0。

其他事件（`agent_thought`、`agent_action` 等）同样不匹配前端的事件常量，但它们走到了 `else` 分支被当作"其他类型事件"处理并添加到 `agent_thoughts` 数组，所以"显示运行流程"面板有数据。

刷新后内容正常是因为刷新时调用分页 API 从数据库加载消息，数据库中的 `answer` 字段是在 `save_agent_thoughts` 中正确写入的（后端累积逻辑不受此 bug 影响）。

**修复**：在所有 SSE yield 语句中使用 `.value` 获取枚举的实际字符串值：

```python
# 修复前
yield f"event: {agent_thought.event}\ndata:{json.dumps(data)}\n\n"

# 修复后
yield f"event: {agent_thought.event.value}\ndata:{json.dumps(data)}\n\n"
```

**涉及文件**（4 个）：
- `my-dify/internal/service/assistant_agent_service.py` — 辅助 Agent 对话
- `my-dify/internal/service/app_service.py` — 应用调试对话
- `my-dify/internal/service/web_app_service.py` — WebApp 对话
- `my-dify/internal/service/openapi_service.py` — OpenAPI 对话

---

## Bug 20：建议问题预测接口返回空数据（`with_structured_output` 与通义千问不兼容）

**现象**：用户与 AI 对话后，前端调用 `/ai/suggested-questions` 接口，返回 `{"code": "success", "data": [], "message": ""}`，建议问题列表为空。

**日志关键信息**：

```
Request body: {'model': 'qwen-plus', 'parameters': {..., 'tools': [{'type': 'function', 'function': {'name': 'SuggestedQuestions', ...}}]}}

Response: {'output': {'choices': [{'message': {'content': '', 'tool_calls': [{'function': {'name': 'SuggestedQuestions', 'arguments': '{}'}}]}}]}}
```

模型返回的 `tool_calls` 中 `arguments` 为 `'{}'`（空对象），`questions` 字段完全没有被填充。

**根因**：`generate_suggested_questions` 使用 LangChain 的 `with_structured_output(SuggestedQuestions)` 方式，该方式底层通过 function calling（tool_calls）让模型填充 Pydantic schema。但通义千问（qwen-plus）对这种 function calling 的支持不够好：

1. 模型收到 `tools` 参数后，识别到需要调用 `SuggestedQuestions` 函数
2. 但模型没有正确理解需要填充 `questions` 字段，返回了空的 `arguments: '{}'`
3. LangChain 将空 arguments 反序列化为 `SuggestedQuestions(questions=[])`（使用了 `default=[]`）
4. 最终返回空列表

这是通义千问模型在 function calling 场景下的已知局限性——对于简单的结构化输出（如生成一个字符串数组），模型倾向于在 `content` 中直接回答而不是填充 tool arguments。

**修复**：放弃 `with_structured_output`，改用普通 prompt + `StrOutputParser` + JSON 手动解析：

```python
# 修复前：使用 with_structured_output（通义千问返回空 arguments）
structured_llm = llm.with_structured_output(SuggestedQuestions)
chain = prompt | structured_llm
suggested_questions = chain.invoke({"histories": histories})
questions = suggested_questions.questions  # → []

# 修复后：直接让模型输出 JSON 数组文本，手动解析
prompt = ChatPromptTemplate.from_messages([
    ("system", "你是一个建议问题生成助手。请根据用户传递的对话历史，预测用户接下来最可能会问的3个问题。"
               "\n每个问题保持在50个字符以内。"
               "\n\n你必须严格按照以下JSON数组格式输出，不要输出任何其他内容："
               '\n["问题1", "问题2", "问题3"]'),
    ("human", "{histories}"),
])
chain = prompt | llm | StrOutputParser()
raw_output = chain.invoke({"histories": histories})

# 解析 JSON（兼容 markdown 代码块包裹的情况）
text = raw_output.strip()
if text.startswith("```"):
    lines = [l for l in text.split("\n") if not l.strip().startswith("```")]
    text = "\n".join(lines).strip()
questions = json.loads(text)  # → ["问题1", "问题2", "问题3"]
```

这种方式不依赖 function calling，直接让模型在 `content` 中输出 JSON 数组，通义千问对此支持良好。

**涉及文件**：
- `my-dify/internal/service/conversation_service.py` — `generate_suggested_questions` 方法

---

## Bug 21：Agent 对话时 `bind_tools` 抛出 `NotImplementedError` 导致线程崩溃

**现象**：应用对话时，后端日志报 `NotImplementedError`，完整调用栈指向 `function_call_agent.py` 第 247 行 `llm.bind_tools(self.agent_config.tools)`。对话线程崩溃，前端无响应。

**根因**：`_llm_node` 中检测 `bind_tools` 是否可用的逻辑有缺陷：

```python
if (
    hasattr(llm, "bind_tools")
    and callable(getattr(llm, "bind_tools"))
    and len(self.agent_config.tools) > 0
):
    llm = llm.bind_tools(self.agent_config.tools)
```

LangChain 的 `BaseChatModel` 基类定义了 `bind_tools` 方法，但默认实现是 `raise NotImplementedError`。因此 `hasattr` 和 `callable` 检查都返回 `True`，但实际调用时抛出异常。

某些模型提供商的 Chat 类（如百度千帆 `QianfanChatEndpoint`、月之暗面 `MoonshotChat`）继承了 `BaseChatModel` 但没有覆写 `bind_tools`。当这些模型的 `features` 中包含 `TOOL_CALL` 时，`app_service.py` 会选择 `FunctionCallAgent`，进而触发此错误。

**修复**：用 `try/except NotImplementedError` 包裹 `bind_tools` 调用，捕获异常后以不绑定工具的方式继续运行：

```python
if (
    hasattr(llm, "bind_tools")
    and callable(getattr(llm, "bind_tools"))
    and len(self.agent_config.tools) > 0
):
    try:
        llm = llm.bind_tools(self.agent_config.tools)
    except NotImplementedError:
        logging.warning("当前模型不支持bind_tools（function calling），将以不绑定工具的方式运行")
```

**涉及文件**：
- `my-dify/internal/core/agent/agents/function_call_agent.py` — `_llm_node` 方法

---

## 功能更新：新增 DeepSeek、豆包、MiniMax 国产模型集成

**背景**：在完成所有 LLM 提供商统一迁移到 `ChatOpenAI` + OpenAI 兼容接口架构后，新增三个国产模型提供商，扩展平台的模型选择范围。

### 新增提供商

#### 1. DeepSeek（深度求索）

- **API Base URL**：`https://api.deepseek.com`
- **模型列表**：
  - `deepseek-chat`：DeepSeek-V3 通用对话模型，128K 上下文，8K 输出，支持 tool_call
  - `deepseek-reasoner`：DeepSeek-R1 推理模型，128K 上下文，64K 输出，仅支持 agent_thought（推理模型不支持 tool_call）

#### 2. 豆包（字节跳动）

- **API Base URL**：`https://ark.cn-beijing.volces.com/api/v3`
- **模型列表**：
  - `doubao-1.5-pro-256k`：豆包1.5-Pro 旗舰模型，256K 上下文，16K 输出，支持 tool_call
  - `doubao-1.5-lite-32k`：豆包1.5-Lite 轻量模型，32K 上下文，4K 输出，支持 tool_call

#### 3. MiniMax

- **API Base URL**：`https://api.minimax.chat/v1`
- **模型列表**：
  - `MiniMax-M1`：MiniMax-M1 推理旗舰模型，百万 Token 上下文，64K 输出，支持 tool_call
  - `MiniMax-Text-01`：MiniMax-Text-01 通用对话模型，百万 Token 上下文，64K 输出，支持 tool_call

### 文件变更

每个新提供商创建了完整的目录结构：
- `__init__.py` — 空初始化文件
- `chat.py` — `ChatOpenAI` 子类，设置对应的 `openai_api_base`
- `positions.yaml` — 模型排序列表
- 各模型 YAML 配置文件 — 定义上下文窗口、输出限制、特性、参数模板
- `_asset/icon.png` — 提供商图标（暂用占位图标）

**后端更新**：
- `my-dify/internal/core/language_model/providers/providers.yaml` — 添加 deepseek、doubao、minimax 三个提供商
- `my-dify/internal/core/language_model/providers/deepseek/` — 完整目录
- `my-dify/internal/core/language_model/providers/doubao/` — 完整目录
- `my-dify/internal/core/language_model/providers/minimax/` — 完整目录

**前端更新**：
- `my-dify-ui/src/models/provider-api-key.ts` — `SUPPORTED_PROVIDERS` 添加三个新提供商

### 当前平台支持的全部提供商（8个）

| 排序 | 提供商 | 标签 | 模型数量 | 说明 |
|------|--------|------|----------|------|
| 1 | tongyi | 通义千问 | 4 | 阿里云，平台默认提供商 |
| 2 | zhipu | 智谱AI | 3 | GLM 系列 |
| 3 | moonshot | 月之暗面 | 2 | Kimi 系列 |
| 4 | deepseek | DeepSeek | 2 | 深度求索，含 R1 推理模型 |
| 5 | doubao | 豆包 | 2 | 字节跳动 |
| 6 | minimax | MiniMax | 2 | 百万上下文 |
| 7 | wenxin | 百度文心 | 3 | ERNIE 系列 |
| 8 | openai | OpenAI | 2 | GPT 系列 |

**注意**：所有提供商的图标目前使用占位图标，上线前需替换为各提供商的官方 Logo。
