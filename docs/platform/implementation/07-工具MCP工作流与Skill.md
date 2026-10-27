# 工具、MCP、工作流与 Skill

[返回模块目录](README.md)

## 统一接入，但不混淆职责

| 能力 | 输入来源 | 执行或使用形式 |
| --- | --- | --- |
| 内置工具 | 平台工具目录与参数 | LangChain Tool |
| API Tool | 简化 OpenAPI 描述及连接参数 | HTTP 调用包装为工具 |
| MCP Tool | Server 配置、发现的工具 Schema | MCP 客户端调用 |
| Workflow | 发布的节点与边 | 编译为 StateGraph，再包装为工具 |
| Skill | 内置目录中的指令及资源文件 | 原生 Skills 能力按需读取 |

Skill 不是独立调度器，工作流不是自由循环的主 Agent，MCP 也不替代平台的资源授权。

## 配置绑定到运行能力

应用配置服务先校验绑定结构、资源归属和可用性。Factory 使用已经解析的配置装配工具；Core 接收可调用对象，不再自行按浏览器传来的资源 ID 查权限。

工具描述决定模型是否选择工具，args schema 决定如何验证输入，平台授权决定调用者是否有权执行。这三个层次需要各自保留，提示词中的“禁止访问”不能替代权限检查。

## MCP 的连接与缓存

`MCPService` 管理 Server 配置和发现到的工具记录；`MCPRuntimeManager` 把数据库配置转换为运行时配置，解密敏感 Header/Env，再构造实际客户端及工具。

运行时工具缓存是进程内缓存，key 使用 Server ID 和更新时间；配置更新后会使用不同 key。它不是 Redis 共享缓存，也不表示远端 Server 永远可用。远端实际工具变化而本地没有刷新时，需要重新发现目录。

同步业务边界调用异步 MCP 时，管理器区分当前线程是否已有事件循环；存在循环时使用另一线程运行协程并等待结果，避免在已有循环里直接 `asyncio.run`。这个桥接仍然会等待，并没有把同步调用变成独立后台任务。

## Workflow 如何变成工具

1. 前端编辑 draft_graph；Service 校验节点、连线、模型配置和引用资源。
2. 调试通过后才允许发布；发布入口再次用 WorkflowConfig 验证。
3. `Workflow` 从配置构造参数 Schema，将 start、llm、template、dataset retrieval、code、tool、http request 等节点加入 StateGraph。
4. 普通边和条件边决定节点调度，编译后 `_run` 调用图并返回输出。
5. 应用把已发布工作流作为一个工具绑定，模型发起调用时使用工作流的输入协议。

画布坐标变化与执行逻辑变化应区别对待；流程图可视化只是配置编辑，真正的执行依据是服务端校验和编译后的图。代码节点的隔离能力取决于节点执行后端，不能从“使用 StateGraph”推导出系统级沙箱。

## Skill 的加载与提示词稳定性

`SkillService` 从 `BuiltinSkillRepository` 读取目录，规范化应用绑定并解析已启用 Skill。运行时投影只提供已授权资源，按稳定顺序提供名称与路径。

框架 SkillsMiddleware 负责原生 Skill 使用方式，平台的 ReloadingSkillsMiddleware 刷新授权资源。文件路径可见不等于可以跨账号任意读取；Skill 内容也不授予数据库或工具权限。

当前目录和应用绑定是代码中能确认的能力，不把它扩大描述成具备任意用户上传、版本签名、全量市场治理的 Skill 平台。

## 技术亮点与失败边界

- 工具、工作流和 MCP 都转为统一可调用对象，应用装配无需复制主 Agent 循环。
- 配置校验与运行授权都保留：资源发布后可能被禁用，历史绑定不能代替当次检查。
- 自动重试只适合可以确认无副作用的读取。主 Agent 的默认工具重试范围是 ls、read_file、glob、grep，不能自动推广到付款、发布等写工具。
- MCP 配置缓存、远端连接存活、工具实际业务成功是三件事；缓存命中不能当成成功证据。

## 阅读入口

| 文件 | 关注点 |
| --- | --- |
| [应用能力校验与转换](../../../internal/service/app/app_config_service.py) | tools/workflows/datasets 绑定与工具构造 |
| [API 工具服务](../../../internal/service/tool/api_tool_service.py) | Provider、接口定义和业务权限 |
| [MCP 服务](../../../internal/service/tool/mcp_service.py) | 创建、刷新、调试和工具目录 |
| [MCP 运行时](../../../internal/core/mcp/runtime/mcp_runtime_manager.py) | 客户端、缓存和异步桥接 |
| [工作流 Service](../../../internal/service/workflow/workflow_service.py) | 编辑、调试、发布 |
| [工作流图](../../../internal/core/workflow/workflow.py) | 参数 Schema、节点、条件边和 compile |
| [Skill 服务](../../../internal/service/skill/skill_service.py) | Catalog、绑定和启用解析 |
