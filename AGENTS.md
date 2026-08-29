# 注释规范

- 注释、模块说明、类和方法 docstring 默认使用简短中文。
- 类 docstring 说明职责；方法 docstring 说明职责、关键输入输出或约束，控制在一至三行。
- 注释只说明非显然的设计原因、职责边界、权限边界、资源生命周期和异常处理，不复述代码。
- 复杂流程可以使用 `# 1.获取...`、`# 2.校验...` 的连续步骤注释；每个方法、类都加上注释说明。

# 代码模块规范

- 一个模块、类或函数只承担一个清晰职责；配置校验、业务执行、外部 I/O、状态管理、事件转换和流程编排必须分离。
- 纯逻辑使用无副作用函数；数据库、网络、文件、队列和模型调用集中在执行层或适配层。
- 编排层只负责组合模块，不实现底层细节；依赖方向必须从高层指向低层，不得循环依赖。
- 测试目录应与源码结构对应，纯逻辑、边界校验、异常处理和资源生命周期应可独立测试。

# 领域分层与数据对象归属

- `internal.entity` 只放跨业务模块复用的领域对象、值对象、领域枚举、领域状态和领域规则。
- `internal.core.agent_v2` 私有的领域对象和值对象统一放在 `internal/core/agent_v2/entity`；只服务 Agent V2
  内部边界的校验与传输对象统一放在 `internal/core/agent_v2/schema`。不得把这些私有对象散落到 `execution`、`assembly`、
  `backends` 或全局 `internal.entity`、`internal.schema`。
- Agent V2 的单次图执行和资源生命周期放在 `execution`，Agent Protocol 事件解析、投影和安全裁剪放在 `protocol`。
- `assembly` 只负责 Deep Agents 图构建、配置和请求级总装配；Workspace、Memory、Skill、Sandbox 上下文构建放在
  `assembly/context`，遥测与 Token 用量框架适配放在 `assembly/observability`。
- Deep Agents Backend 实现放在 `backends`，LangGraph Checkpointer、Store Provider 和 Workspace 文件仓储放在
  `persistence`，模型调用钩子放在 `middleware`。禁止新建含义重叠的 `agents`、`events`、`context` 或 `storage` 平行包。
- 业务状态常量应与对应领域实体放在 `entity`，例如 `queued`、`running`、`completed`、`failed`、`cancelled`
  和终态集合。不要把所有常量不加区分地放进 `entity`。
- `internal.schema` 放边界数据传输对象和数据校验规则，包括 HTTP 请求、HTTP 响应、Agent Protocol command、SSE
  订阅参数、分页参数和重放批次等。Schema 负责解析、校验和序列化，不负责执行数据库或 Agent 业务。
- `internal.model` 只放数据库 ORM 模型和持久化字段映射。ORM Model 不等同于领域 Entity，也不应被 Handler 当作响应 DTO 直接返回。
- `internal.repository` 放数据库以外的外部存储适配器和访问实现，例如 Redis Stream、Redis Pub/Sub、缓存、对象存储或消息系统。Repository
  接收外部客户端依赖，不得在内部通过全局 Injector 查找依赖或自行创建客户端。
- `internal.service` 负责业务用例编排、权限校验、事务边界和领域流程协调；Service 可以调用 Repository、Model 和领域对象，但不应承载
  Redis 协议细节、HTTP 响应格式或 Schema 的底层校验实现。
- `internal.handler` 只负责 HTTP 路由适配、登录鉴权入口、路径/查询/请求体解析和统一响应封装。Handler 不实现业务规则、数据库查询、Agent
  执行、Redis 操作或事件投影。
- `internal.repository`、`internal.service` 等需要外部资源时，优先使用构造函数依赖注入，例如 `redis_client: Redis`
  ；只有对象明确由业务代码手动创建且无法纳入 Injector 管理时，才允许使用 `injector.get(...)`，并应保持局部和可替换。
- 协议方法名、字段名和传输格式常量应与对应 Schema 或协议模块放置；Redis key 前缀、TTL、Stream 长度等基础设施参数应放在
  Repository 或配置模块；SSE 心跳、分页默认值等运行参数应放在配置或对应 Service/Schema，而不是 Entity。
- 同一业务对象在不同层可以有不同表示：Schema 表示输入输出，Entity 表示运行时领域状态，Model 表示数据库记录；禁止为了复用类型跨层直接替代。
- 包的 `__init__.py` 只导出该包稳定的公共 API。实现细节、内部别名和仅用于兼容的转发模块不应作为公共导出；跨包引用应从职责所属包导入。
- Schema 请求对象统一以 `Req` 结尾，响应对象统一以 `Resp` 结尾，例如 `GetConversationMessagesWithPageReq`、`GetConversationMessagesWithPageResp`；不要使用没有方向含义的 `Input`、`Output` 或裸业务名作为跨边界 DTO 名称。

# Schema 封装与校验规范

- Schema 只负责边界数据结构、解析、校验和序列化，不执行数据库查询、权限判断、Agent 调用、事务提交或 Redis 操作。
- 所有跨 HTTP、SSE、Agent Protocol 或内部服务边界传递的结构化数据，都应定义明确的 Schema；禁止让裸 `dict` 在多层之间长期流转。
- 请求 DTO 统一以 `Req` 结尾，响应 DTO 统一以 `Resp` 结尾。`Input`、`Output` 和没有方向含义的裸业务名只允许用于局部内部变量，不得作为跨边界 DTO 名称。
- Handler 将路径参数、查询参数和请求体解析为对应的 `Req` 后再调用 Service；Service 接收已经规范化的数据，不重复解析 Flask 请求或原始 query string。
- Handler 不直接返回 ORM Model、Entity 或 Service 内部对象；Service 返回领域结果后，由 Handler 使用对应 `Resp` Schema 投影并序列化。
- 协议响应、SSE 事件载荷、重放批次和错误响应也属于边界对象，必须定义对应 Schema，不能临时拼接不稳定的字典结构。
- 校验规则放在最靠近数据边界的 Schema 中：类型、必填项、格式、范围、互斥字段和默认值由 Schema 统一处理；跨字段的业务约束由 Service 或 Entity 处理。
- Schema 校验失败必须转换为项目统一的校验异常或由 Handler 统一转为 `validate_error_json`；不要在各个接口中手写不同的错误格式。
- Schema 序列化应显式指定项目要求的模式（例如 `model_dump(mode="json")`），确保 UUID、枚举、日期和嵌套对象得到稳定的协议表示。

# 结构化数据校验与避免重复造轮子

- 只要数据跨 Handler、Service、Agent Core、Repository 或外部协议边界传递，且包含多个字段、嵌套结构、枚举、UUID、URL、范围或互斥关系，就必须优先定义 Pydantic `BaseModel`、`RootModel` 或项目已有的 Schema；不要让 `dict[str, Any]` 作为长期传输类型。
- Pydantic 模型负责结构性校验和规范化：字段类型、必填项、默认值、长度、格式、枚举、额外字段、列表元素、重复项以及同一对象内部的跨字段约束。优先使用 `StrictStr`、`StrictInt`、`StrictBool`、`Literal`、`UUID`、`AnyUrl` 和 `Field` 等声明式能力，避免先用 `str()`、`bool()`、`int()` 强制转换后再校验。
- 禁止为已经有 Pydantic 或标准库能力的数据类型重复手写 `isinstance`、`.get()`、`set(...)`、字符串拆分和多层 `try/except` 校验流程。确有特殊规则时，将规则写入模型 validator，并为不能由模型表达的业务条件保留清晰的 Service 校验。
- 不要为了处理结构化输入自行实现通用校验框架、错误聚合器、UUID/URL/枚举解析器或字段默认值系统；先查找并复用项目已有 Schema、Entity、Pydantic 模型和成熟库。
- 一个 Pydantic 模型只描述一个稳定的数据边界。HTTP 请求/响应和 Agent Protocol 数据放在 `internal.schema`；只属于 Agent Core 的运行时配置和值对象放在 `internal/core/agent_v2/entity`；不要为了复用而让 ORM Model 充当 DTO。
- Service 只负责 Pydantic 无法表达的业务校验，例如租户归属、权限、数据库引用存在性、发布状态、资源可用性、循环引用和事务约束。Service 不应重新解析原始请求或重复执行 Schema 已完成的字段类型校验。
- Pydantic `ValidationError` 只在边界处转换为项目统一的校验异常，并保留字段路径和原异常原因；不要在每个字段周围单独捕获异常，也不要用 `except Exception` 把编程错误伪装成“配置错误”。
- `try/except` 只用于明确的异常边界：资源清理、第三方异常转换、可预期的基础设施失败或需要补偿的事务步骤。捕获后必须保留 `from exc`、记录必要上下文，并且不能让清理异常覆盖原始异常。
- 运行时收到的数据如果已经通过 Schema 或 Pydantic 模型校验，应直接使用其类型化字段；不要在后续 Service、Assembler 或 Agent Core 中退回到对同一数据的多套字典校验。
- 只有可变累加器、轻量内部状态或明确不需要验证的内部对象才使用 `dataclass`；需要边界解析、类型约束、不可变配置或序列化的对象应继承 `BaseModel`。

# 分页规范

- 普通页码分页使用 `pkg.paginator.PaginatorReq`；基于唯一标识或时间线的游标分页使用 `pkg.paginator.CursorPaginatorReq`。
- 不在具体业务 Schema 中重复定义通用分页请求；分页游标、页码、页大小和范围校验集中在 `pkg.paginator`。
- Handler 负责把分页请求解析为 paginator 对象，Service 只接收规范化后的页码或游标，不处理原始查询字符串。
- 游标分页必须定义稳定排序、方向和去重规则；响应应通过对应 `Resp` Schema 返回下一页游标及数据，不能依赖前端猜测游标含义。

# 异常与资源处理规范

- Handler 负责请求解析、Schema 校验错误映射和统一响应封装；不在 Handler 中捕获并吞掉业务异常。
- Schema 校验异常应转换为项目统一异常（如 `ValidateErrorException`），并保留字段级错误信息；响应状态码和错误结构由统一响应工具处理。
- Service 负责业务前置条件、权限和状态冲突校验。可预期的业务失败应抛出项目定义的领域/业务异常，不返回含糊的布尔值或静默忽略错误。
- Service 不使用宽泛 `except Exception` 隐藏异常。确需在边界层重新包装时，必须保留原异常作为 cause，并记录足够上下文。
- Repository 只捕获可预期的外部资源异常（例如连接、超时或不存在），并返回明确的降级信号或抛出可识别的基础设施异常；不得把所有异常转换成“成功”。
- 不在 Handler、Schema 或 Repository 中直接打印堆栈；统一交给应用异常处理器和日志配置，面向客户端的错误信息不得泄露密钥、授权信息、提示词、文件内容或内部堆栈。
- 流式响应、数据库会话、Redis 订阅和文件等资源必须在 `finally` 中释放，释放逻辑要幂等，并兼容客户端提前断开、取消和生成器异常。
- 取消、超时和客户端断开属于正常控制流时，应转换为明确的终态/事件；不要以未处理异常结束生成器，也不要让资源清理依赖仍然存在的 Flask 请求上下文。

# Agent Protocol 与运行生命周期规范

- 所有面向前端或外部调用方的 Agent 对话统一使用 Agent Protocol 的 `commands`、`stream`、`state` 和 `messages` 边界；禁止新增旧 `/chat`、自定义 SSE 事件名或第二套流式传输协议。
- 每次 `run.start` 必须创建并返回持久化 `AgentRun.id`。运行、事件、取消、幂等和重放统一使用 `run_id`，不得重新引入临时 `task_id` 身份体系。
- 客户端取消使用官方 SDK 的 `stream.stop()`，服务端按同一作用域下的 `/threads/{thread_id}/runs/{run_id}/cancel` 处理；不得通过“查询线程最新 Run”的模糊接口代替明确的 run 取消。
- PostgreSQL `AgentRun` 是运行状态权威。Redis 只承担短期事件重放、跨 worker Run 通知和按 `run_id` 的低延迟取消信号，不得维护另一套业务运行状态。
- Deep Agents/LangGraph v3 原始事件只能在执行边界统一投影为 Agent Protocol 事件；Handler、业务 Service 和前端不得分别维护重复的事件翻译器。

# 数据库迁移规范

- 功能开发涉及数据库结构变化时，只修改 `internal.model` 中的 ORM Model，不手写、创建或生成 `internal/migration/versions` 下的迁移文件。
- 不主动执行 `flask db migrate`；迁移文件统一由用户在确认 Model 定义后自行执行该命令生成。
- 不主动执行 `flask db upgrade`、`flask db downgrade` 或直接修改数据库结构、数据的 SQL；只有用户明确要求执行具体迁移操作时才允许执行。
- 不修改已经存在的 revision、`down_revision` 或迁移链来适配未落库代码。发现 Model 与数据库或迁移链不一致时，只报告差异和风险，由用户决定迁移生成与执行时机。

# Agent 文件存储与 Sandbox 规范

- Agent 业务文件内容统一保存在对象存储，数据库只保存租户、应用、会话、逻辑路径、对象 key、MIME、大小、摘要、修订号和审计时间等元数据；禁止把文件二进制或完整文本写入 ORM 字段。
- 对象存储操作统一放在 `internal.repository`，接收已注入的客户端或配置；Service 负责协调“先上传对象、再提交元数据”和失败补偿，Handler、Model 与 Deep Agents Backend 不得直接调用 COS SDK。
- 前端文件列表只返回元数据；文本预览必须限制读取字节数，媒体和下载使用短期签名 URL，禁止向前端暴露永久公开 COS 地址或对象存储凭据。
- COS 是 Workspace 文件内容的权威存储，PostgreSQL 是元数据权威；Sandbox 只是单次 Agent 执行的临时工作副本，不得作为持久化来源。
- Sandbox 启动时从权威 Workspace 物化当前文件，结束时按路径和内容摘要清单只同步新增、变更和删除；禁止每次全量回写、重复生成未变化修订或用 Sandbox 中的陈旧文件覆盖权威数据。
- 启用 Sandbox 不得关闭 `FilesystemPermission`。Skill 路径只读、Workspace 路径可读写、其余路径默认拒绝；同时要说明文件工具权限无法约束 `execute`，Skill 的 Sandbox 副本还需使用容器或操作系统级只读挂载保护。
- 内置 Skill 包保存在版本控制的资源目录，应用配置只保存版本绑定；自定义 Skill 内容应使用独立的对象存储 key 和 Skill 元数据表，不得混入 Workspace 文件表，也不得在 Skill 文件中保存密钥。
- 不预留没有生产者、消费者和明确协议的虚拟目录，例如 `/.system/**`、`/.audit/**`；审计数据应进入日志、遥测或运行记录，系统上下文应由 Prompt 和 Runtime Context 注入。

# Agent 分层记忆规范

- LangGraph Checkpointer 只负责 `tenant + app + principal + thread` 作用域的会话状态；跨线程长期记忆使用 Deep Agents `MemoryMiddleware`、`CompositeBackend`、`StoreBackend` 和 LangGraph Store，不得用 Redis 或业务消息表重建第二套记忆机制。
- 长期记忆命名空间固定包含 `tenant_id + app_id + principal_id`，不得包含 `thread_id`；`principal_id` 必须由服务端解析为稳定 UUID，不得使用 `None`、`account`、`public` 等应用共享兜底。
- 应用编排通过已有 `long_term_memory.enable` 控制长期记忆。关闭时不得初始化 Store、注册 `/memories/` 路由、注入 Memory source、提示记忆能力或允许记忆路径；关闭只停用能力，不删除已有数据。
- `CompositeBackend` 会把 `/memories/MEMORY.md` 的路由前缀移除后交给 StoreBackend，因此 Store key 为 `/MEMORY.md`。HTTP Repository 必须统一转换虚拟路径与 Store key，不得自行恢复前缀或让两条链路保存重复记录。
- 长期记忆只保存小体量 UTF-8 文本，不承担业务文件、媒体、附件和交付物存储。默认上限为单文件 64 KiB、每主体 100 个文件、每主体总计 1 MiB，HTTP Service 与 Agent Backend 必须执行同一组限制。
- `/memories/MEMORY.md` 保存稳定偏好和事实；应用行为指令属于 AppConfig 或 Skill，不使用 `/memories/AGENTS.md`。知识材料进入 Dataset/RAG，业务产出文件进入 Workspace/COS。
- 删除会话或消息不得删除跨会话记忆；删除应用时按 `tenant + app` 命名空间前缀清理所有主体记忆。禁用开关不触发清理。
