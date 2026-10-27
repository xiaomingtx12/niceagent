# ④ 工具、MCP、工作流与 Skill：能力怎样变成一次调用

[返回组件地图](00-组件地图.md) · 业务链路见 [工具、MCP、工作流与 Skill](../platform/implementation/07-工具MCP工作流与Skill.md)

## 这块组件在干什么

模型看到的是一组 LangChain Tool。平台要保证：描述决定「会不会选」、schema 决定「参数合不合法」、授权决定「能不能执行」。提示词里的「禁止访问」不能替代后两层。

## 当前怎么做

### 五类能力

| 能力 | 配置来源 | 运行时形态 |
|---|---|---|
| 内置工具 | 目录 YAML + Provider | LangChain Tool |
| API Tool | 简化 OpenAPI + 连接 | HTTP 包装为工具 |
| MCP Tool | Server 配置 + 发现 Schema | MCP 客户端调用 |
| Workflow | 已发布节点和边 | 编译 StateGraph，再包成工具 |
| Skill | 内置目录的指令和资源文件 | 原生 Skills 按需读文件 |

Skill 不是调度器。Workflow 不是自由循环的主 Agent。MCP 不替代平台授权。

Factory 只装配已经校验归属的绑定；Core 收可调用对象。草稿路径可 `batch_lookup=True` 把 API/MCP 查询合成一次，输出仍保持原顺序和 ID，避免改等待表单的定义指纹。

### 重试和副作用

平台**不**给工具打 `side_effect_level` 元数据。替代策略：

- 自动重试白名单只有 `ls` / `read_file` / `glob` / `grep`
- 写入和未知业务工具：异常无法证明上次有没有生效，故不自动重试
- 飞书 / Notion 等商业 HTTP：超时或失败的文案要求先查询结果再决定是否重试；限制响应体约 2MB，禁止跟随重定向

工具调用 JSON 损坏（`ToolCallValidationMiddleware`）直接失败，不修复后执行。

### MCP

`MCPService` 管配置和发现记录；`MCPRuntimeManager` 解密 Header/Env，构造客户端。进程内工具缓存 key = Server ID + 更新时间，配置一变 key 变。不是 Redis，也不表示远端永远可用。

同步边界调异步 MCP：当前线程已有 event loop 时，另开线程 `asyncio.run` 再 join。这仍是同步等待，不是后台任务。

### Workflow 变工具

草稿校验节点、边、模型和引用 → 调试通过才允许发布 → `Workflow` 把 start / llm / template / retrieval / code / tool / http 编进 StateGraph → 应用绑定已发布工作流。画布坐标不是执行依据。代码节点的隔离取决于执行后端，不能从「用了 StateGraph」推出系统级沙箱。

### Skill

`SkillService` 读内置目录，规范化绑定。运行时投影只暴露已授权包，名称路径稳定排序。`ReloadingSkillsMiddleware` 每轮刷新授权元数据；官方 SkillsMiddleware 负责怎么用。路径可见 ≠ 跨账号可读。当前没有任意用户上传、签名和市场治理。

## 明确不保证

- 没有 Host 注入的位置绑定幂等键；提交层幂等（`input_message_id`）挡的是重复开 Run，不是重复调写工具
- MCP 不是带 OBO / 参数级策略 / DLP 的控制面网关
- 工具改名会让只读重试白名单失效

## 代码入口

| 文件 | 读什么 |
|---|---|
| [`builtin_provider_manager.py`](../../../internal/core/tools/builtin_tools/providers/builtin_provider_manager.py) | 内置工具加载 |
| [`api_provider_manager.py`](../../../internal/core/tools/api_tools/providers/api_provider_manager.py) | API 工具 |
| [`mcp_runtime_manager.py`](../../../internal/core/mcp/runtime/mcp_runtime_manager.py) | MCP 客户端与缓存 |
| [`business_http.py`](../../../internal/core/tools/builtin_tools/business_http.py) | 写操作失败语义 |
| [`skill_context.py`](../../../internal/core/agent_v2/assembly/context/skill_context.py) | Skill 投影 |
| [`tool_call_validation_middleware.py`](../../../internal/core/agent_v2/middleware/tool_call_validation_middleware.py) | 拒绝损坏调用 |
