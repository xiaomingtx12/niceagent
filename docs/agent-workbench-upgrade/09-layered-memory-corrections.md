# Agent 分层记忆机制纠正说明

## 1. 旧实现的问题

旧代码虽然使用了 LangGraph Store，但没有形成清晰的记忆边界：

- 运行时无条件初始化 Store，并始终向 Agent 暴露 `/memories/`；应用无法在编排阶段关闭长期记忆。
- 命名空间使用 `end_user_id or "account"`。无法解析终端用户时会回退到应用账号共享空间，多个调用主体可能读写同一份记忆。
- Agent 通过 `CompositeBackend` 写入 `/memories/MEMORY.md` 时，StoreBackend 实际收到的 key 是 `/MEMORY.md`；HTTP 接口却按带前缀 key 查询，两条链路访问的不是同一条记录。
- Service 同时包含路径、Entity、编码、MIME、哈希、Store 重试和响应投影，Handler 又手写 JSON 校验并直接调用 `to_dict()`。
- 记忆接口支持 base64、下载和二进制 MIME 推断，把“长期文本记忆”误做成了第二套业务文件存储。
- 默认来源使用 `/memories/AGENTS.md`，容易把应用行为指令与用户长期记忆混为一谈。
- Agent 文件工具写入没有单文件、文件数和总容量边界。

这些问题会造成租户边界不稳定、管理接口与 Agent 数据不一致、Store 无谓连接，以及记忆与 Workspace 职责重叠。

## 2. 新的分层边界

```text
L1 会话状态
  LangGraph Checkpointer
  tenant + app + principal + thread

L2 跨会话长期记忆
  Deep Agents MemoryMiddleware
    -> CompositeBackend /memories/
    -> 受限 StoreBackend
    -> LangGraph PostgresStore
  tenant + app + principal

L3 业务知识
  Dataset / RAG

业务产出文件
  Workspace 元数据 + COS 内容
```

Checkpointer 负责同一线程的消息、Todo 和图状态恢复。长期记忆只保存跨线程仍然有价值的小体量文本，例如稳定偏好、用户确认的事实和长期项目约定。知识库继续走 RAG；报告、图片、代码包等交付物继续写入 `/workspace/` 并由 COS 持久化。

## 3. 多租户隔离

长期记忆命名空间固定为：

```text
("mydify", "agent-memory", tenant_id, app_id, principal_id)
```

- `tenant_id` 来自当前应用所属账号。
- `app_id` 阻止不同应用共享记忆。
- `principal_id` 必须由服务端解析为具体 UUID，不能为空，也没有公开或账号共享兜底。
- `thread_id` 不进入长期记忆命名空间，因此同一主体可跨会话使用记忆。
- SubAgent 继承主 Run 的 `AgentRunContext` 和记忆 Backend，不创建第二套身份。

调试和创建者入口使用当前账号 ID；WebApp 和微信入口使用服务端解析的 EndUser ID；其他内部或 API 入口使用本次调用中已经认证的 `user_id`。身份解析发生在业务装配层，前端不能指定 `principal_id`。

## 4. 编排开关如何生效

草稿和发布配置统一使用：

```json
{
  "long_term_memory": {
    "enable": true
  }
}
```

草稿配置控制调试运行，发布配置控制 WebApp、API 和微信运行。基础设施配置 `AGENT_MEMORY_BACKEND=postgres` 只决定启用时使用哪种 Store，不代表每个应用都开启记忆。

关闭时：

1. 不调用 Store provider，不创建 PostgresStore 连接。
2. `CompositeBackend` 不注册 `/memories/` 路由。
3. 不向 `create_deep_agent()` 传入 memory source 和 Store。
4. 文件工具权限明确拒绝 `/memories/**`。
5. 系统提示词不介绍长期记忆。

开启时才装配上述能力，并把 `/memories/MEMORY.md` 作为 Deep Agents 原生 MemoryMiddleware 的默认来源。关闭不会删除已有记忆；再次开启后仍可读取。删除应用时才按应用命名空间前缀清理所有主体记忆，删除会话或消息不会清理跨会话记忆。

## 5. Agent 与 HTTP 为什么能看到同一份数据

`CompositeBackend` 会移除匹配到的路由前缀：

```text
Agent 虚拟路径                 Store key
/memories/MEMORY.md    <->    /MEMORY.md
/memories/profile.md   <->    /profile.md
```

这是 Deep Agents 官方 CompositeBackend 的正常语义，不应通过自定义 Backend 把前缀加回去。本次由 `AgentMemoryRepository` 统一完成双向转换，HTTP 管理接口始终暴露虚拟路径，Store 中只保存相对 key。

## 6. 保留的框架能力与平台补充

代码没有重写 Deep Agents 的记忆机制：

- `MemoryMiddleware` 负责在模型调用前加载记忆来源并注入上下文。
- `FilesystemMiddleware` 继续提供 `read_file`、`write_file`、`edit_file` 等标准工具。
- `CompositeBackend` 负责虚拟路径路由。
- `StoreBackend` 负责 LangGraph Store 文件格式和读写语义。
- `PostgresStore` 负责跨进程、跨线程持久化。

平台只增加 Deep Agents 不负责的业务边界：可信主体解析、应用开关、HTTP 授权、路径转换、容量限制、连接重试和应用删除清理。受限 Backend 继承官方 `StoreBackend`，同步和异步 `write/edit/upload` 都执行相同约束。

## 7. 内容与配额规则

长期记忆仅支持 UTF-8 文本：

- 单文件最大 64 KiB。
- 每个租户、应用、主体最多 100 个文件。
- 每个作用域总容量最大 1 MiB。
- Agent Backend 和 HTTP Service 都校验相同规则。
- `upload_files` 遇到非 UTF-8 字节直接拒绝，不再转换为 base64。

业务文件不应写入 Memory。需要前端预览或下载的产物必须进入 Workspace/COS；Memory API 因而移除了下载路由、`BytesIO`、MIME 推断、base64、内容哈希和伪造的文件 ID。

## 8. 代码职责

```text
entity/agent_memory.py
  身份、虚拟路径、Store key、容量规则和记忆文件 Entity

persistence/agent_memory_repository.py
  LangGraph Store CRUD、连接重试和应用前缀清理

backends/agent_memory_backend.py
  官方 StoreBackend 的文本与配额保护

schema/memory_file_schema.py
  Req/Resp 校验和显式序列化

service/memory_file_service.py
  HTTP 用例、主体作用域与领域异常映射

handler/memory_file_handler.py
  登录身份、请求解析和统一响应
```

这次改造不涉及 ORM 字段变化，没有生成或执行数据库迁移。`long_term_memory` 继续使用已有 JSONB 字段，配置键统一为 `enable`。
