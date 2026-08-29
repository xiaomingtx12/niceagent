# Workspace 存储与 Sandbox 同步纠正说明

## 1. 本次纠正解决什么问题

旧实现把 `WorkspaceFile.content` 作为 `LargeBinary` 写入 PostgreSQL，同时保留
`storage_backend="db"`。这会让业务库承担大对象吞吐、备份和膨胀成本，也让 Handler、
Service、Deep Agents Backend 都直接依赖 ORM 中的文件内容。

Sandbox 侧则在每次运行开始全量上传 Workspace，结束时再次全量下载和写回。未变化文件
也会生成版本，复用 Sandbox 时还可能把旧文件重新写回权威存储。Skill 虽然通过只读
Backend 暴露，但启用 Sandbox 后文件工具权限被整体置空，且 Sandbox 内的 Skill 副本
没有文件系统只读保护。

此外，`/.system/**`、`/.audit/**` 只有路径判断和权限规则，没有任何生产者、消费者或
协议定义，属于无效的预留设计。

## 2. 纠正后的权威边界

```text
前端
  -> WorkspaceFileHandler        请求解析、Schema 校验、统一响应/重定向
  -> WorkspaceFileService        会话归属、访问授权、预览和下载策略
  -> WorkspaceFileStorageService COS 与 SQL 的一致性协调
       -> CosObjectStorageRepository  文件字节
       -> WorkspaceFileRepository     文件元数据

Agent / Deep Agents
  -> AgentWorkspaceBackend       BackendProtocol 适配
  -> WorkspaceFileStorageService
       -> COS + PostgreSQL
```

- COS 是持久化文件内容的唯一权威来源。
- PostgreSQL 只保存当前逻辑文件的元数据和对象 key。
- Sandbox 是一次 Agent 执行期间的可丢弃工作副本，不是业务存储。
- Skill 内置包保存在 Git 管理的资源目录，通过只读路径投影给 Agent。

`WorkspaceFile` 当前字段包括作用域、逻辑路径、`object_key`、MIME、大小、SHA-256、
`revision`、创建人、删除状态和时间。模型不再包含文件二进制，也不再使用同时支持
`db/cos` 的过渡字段。

## 3. 文件写入原理

一次 Workspace 写入按以下顺序执行：

1. 校验账号、应用、会话和 `/workspace/` 虚拟路径。
2. 计算内容 SHA-256；内容未变化时直接复用当前元数据，避免无意义修订。
3. 使用不可变、不可复用的 COS object key 上传新内容。
4. 在数据库事务中锁定逻辑路径，检查配额并新增或更新元数据。
5. 数据库提交失败时，补偿删除刚上传的新对象。
6. 提交成功后，尽力删除被替换的旧对象；清理失败只记录日志，不回滚已经生效的新版本。

SHA-256 是内容摘要，不是加密。它用于判断内容是否变化和校验同步结果，不用于隐藏文件
内容。COS 分片上传的 ETag 不一定等于内容哈希，因此业务同步使用 SHA-256 更稳定。

## 4. 前端预览与下载

文件列表只返回 Schema 定义的元数据，不返回 ORM，也不暴露 `object_key`。

- 文本、Markdown、代码和 JSON 等文件通过 COS Range 请求读取有限字节数。
- 响应包含 `truncated`，前端可以明确展示预览是否截断。
- 图片、音频、视频、PDF 和普通下载使用短期 COS 签名 URL。
- 下载接口在完成业务授权后返回 302 到签名 URL，不拼接永久公网地址。

默认预览上限由 `WORKSPACE_PREVIEW_MAX_BYTES` 控制，签名 URL 有效期由
`WORKSPACE_DOWNLOAD_URL_TTL_SECONDS` 控制。

## 5. Sandbox 增量同步原理

运行开始时，以 COS/PostgreSQL 为权威重建 `/workspace`：

1. 清理复用 Sandbox 中上次运行留下的 Workspace 和 Skill 投影。
2. 下载当前会话可见的 COS 文件并物化到 `/workspace`。
3. 记录 `path -> content_sha256` 初始清单。
4. 上传本次授权的 Skill 包，并把 Skill 目录设置为文件系统只读。

运行结束时：

1. 枚举 Sandbox 最终 `/workspace` 文件并下载内容。
2. 计算最终 SHA-256，与初始清单比较。
3. 只上传新增和内容发生变化的文件。
4. 初始存在但最终缺失的路径提交软删除，并清理 COS 对象。
5. 未变化文件不写数据库、不生成新修订、也不重复上传 COS。

这套同步解决的是“运行期文件系统”和“业务持久化”之间的数据交换，不改变 Agent Protocol
的 `commands/stream/state/messages` 流式传输机制。

## 6. Skill 存储与权限

内置 Skill 继续保存在 `resources/agent_skills/builtin`，应用配置只保存名称、版本、摘要和
启用状态。未来自定义 Skill 应使用独立的 Skill 元数据表和 COS package key，不能存进
Workspace 表，也不能把密钥写入 `SKILL.md`。

文件工具权限在 Sandbox 和非 Sandbox 模式下保持一致，按首条匹配规则执行：

1. 拒绝写入 `/skills/**` 和 `/subagent_skills/**`。
2. 始终允许读取已投影的 Skill 和 Workspace，并允许写入 Workspace。
3. 只有应用编排启用长期记忆时，才允许读写 `/memories/**`；关闭时明确拒绝。
4. 拒绝其他路径的读写。

`FilesystemPermission` 只约束 Deep Agents 的文件工具，不能拦截 `execute` 中运行的 shell
或 Python。因此代码同时给 Sandbox Skill 副本设置只读权限。若生产环境要求不可绕过的
强隔离，还必须由 Sandbox provider 使用只读挂载、非特权运行用户和容器策略保证；仅靠
应用层 `chmod` 不能替代容器安全边界。

## 7. 删除的无效设计

本次移除了 `/.system/**`、`/.audit/**` 及其 Workspace 变体，也移除了对应的保留路径
判断。系统上下文由 Prompt 和 Runtime Context 注入；审计信息进入日志、遥测和
`AgentRun`，不伪装成文件目录。

## 8. 数据库迁移说明

代码只修改了 ORM Model，没有创建、修改或执行迁移文件。当前测试数据库无需兼容旧
Workspace 内容，建议确认模型后由开发者执行：

```powershell
flask --app app.http.app db migrate -m "store workspace content in cos"
flask --app app.http.app db upgrade
```

生成后应检查迁移是否删除 `content`、`storage_backend`、旧版本唯一约束，并新增
`object_key`、`revision` 和当前逻辑路径唯一约束，再决定是否执行升级。
