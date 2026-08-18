# Skill、文件系统与 Backend 重构规划

> 本文是 Agent V2 Backend 重构的当前设计基线。
> 先确定 DeepAgents 原生 Backend 的职责，再调整代码目录和运行时装配。
> 本轮只调整规划，不修改测试文件。

## 1. 先建立正确的 Backend 心智模型

DeepAgents 的 Backend 是文件数据平面，不是 Agent、工具、权限策略或业务
Service。DeepAgents 原生 FilesystemMiddleware 负责提供文件工具，工具在执行
时调用 Backend；项目只需要提供合适的文件数据来源和业务隔离边界。

```text
模型
  -> DeepAgents 原生文件工具
  -> FilesystemMiddleware
  -> BackendProtocol
  -> StateBackend / StoreBackend / Sandbox / MyDify 业务适配器
```

必须区分四件事：

| 概念 | 负责什么 | 不负责什么 |
|---|---|---|
| `BackendProtocol` | 读写、列表、搜索、上传、下载等文件数据操作 | 不决定模型是否能看到工具 |
| `FilesystemMiddleware` | 暴露原生文件工具并应用 `permissions=` | 不负责租户数据库查询 |
| `CompositeBackend` | 按虚拟路径把操作路由到不同 Backend | 不实现持久化和业务 ACL |
| MyDify Backend 适配器 | 租户隔离、路径映射、数据库版本、只读边界 | 不重写 DeepAgents 文件工具 |

因此，Backend 目录应该只放实现或适配 `BackendProtocol` 的代码。
上下文装配器只负责选择 Backend、生成路由和传递 `skills=`、`memory=`；
它们不应成为 Backend 实现的归属目录。

## 2. `SkillCatalogBackend` 到底是什么

`internal/core/agent_v2/backends/skill_catalog.py` 不是 DeepAgents 官方模块。
`SkillCatalogBackend` 是 MyDify 自己写的一个很薄的 Backend 适配器：

1. 接收 `SkillService` 从内置 Skill 目录加载出的已授权 Skill 包；
2. 把每个包转换成虚拟文件：

```text
/skills/csv-analysis/SKILL.md
/skills/csv-analysis/scripts/summarize_csv.py
/skills/csv-analysis/references/...
```

3. 通过 `ReadOnlyCatalogBackend` 实现 `read/ls/glob/grep/download`；
4. 拒绝 `write/edit/upload`；
5. 让 DeepAgents 原生 `SkillsMiddleware` 使用 `skills=["/skills/"]`
   渐进式读取 Skill，而不是把完整 Skill 正文直接拼进 system prompt。

它本质上是 **Skill 文件目录的 Backend**，不是 Context Assembler。

对应关系：

```text
runtime/assembly.py
  -> 负责 Skill 绑定校验、SubAgent 子集校验、虚拟前缀和文件清单

backends/skill_catalog.py
  -> 负责 Skill 包的只读 Backend 协议实现

runtime/factory.py
  -> 只把 /skills/ 和 /subagent_skills/ 传给原生 skills=
```

长期记忆不再有 `MemoryProfileBackend` 或事实到 Markdown 的第二套投影；
`runtime/memory.py` 只负责创建原生 `StoreBackend` 和 namespace factory。

## 3. 目标目录边界

```text
internal/core/agent_v2/
├── backends/
│   ├── read_only.py        # 通用只读虚拟文件目录
│   ├── skill_catalog.py    # 内置 Skill 包只读 Backend
│   ├── workspace.py        # workspace_files 到 BackendProtocol 的适配
│   └── sandbox.py          # 官方 Sandbox provider 生命周期适配
├── runtime/
│   ├── assembly.py          # 请求级资源装配和路由投影
│   ├── memory.py            # StoreBackend 与 namespace
│   ├── factory.py           # create_deep_agent 参数装配
│   └── subagents.py         # 平台 SubAgent 配置转换
└── persistence/
    ├── checkpoint.py       # LangGraph Checkpoint Provider
    ├── store.py            # LangGraph Store Provider
    └── workspace.py        # workspace_files 版本仓储
```

硬规则：

- `persistence/` 不实现 `BackendProtocol`，只负责权威存储。
- `backends/` 不查询请求外的租户身份，也不负责 Skill 绑定决策。
- `context/` 不定义新的 Backend 协议，不复制 DeepAgents 文件工具。
- `runtime/` 不拼装路由细节，只调用统一的资源装配结果。

## 4. Backend 类型和所有权

### 4.1 DeepAgents 原生 Backend

优先使用以下原生 Backend：

| Backend | 适用场景 |
|---|---|
| `StateBackend` | 当前线程内的临时文件和框架内部文件 |
| `StoreBackend` | 跨线程、跨请求的长期文件，例如 Memory |
| `CompositeBackend` | 按虚拟路径组合多个 Backend |
| 官方 Sandbox Backend | 隔离执行、文件操作和原生 `execute` |

不要因为项目有数据库，就把所有文件都强行实现成数据库 Backend。
数据库只应承载需要跨请求、可审计、可配额和可恢复的业务数据。

### 4.2 MyDify Workspace Backend

`MyDifyWorkspaceBackend` 是必要的业务适配器，因为 Workspace 需要：

- account/app/conversation 作用域；
- 虚拟路径到 `workspace_files` 的映射；
- 版本追加、软删除和冲突重试；
- 配额、路径保留段和审计 telemetry；
- Creator 与终端用户的访问边界。

它只实现 BackendProtocol，不实现文件工具、不实现摘要、不实现命令执行。

`WorkspaceRepository` 保持在 `persistence/`，由 Workspace Backend 调用。
Repository 不应该被 Agent、Middleware 或入口 Service 直接当作 Backend 使用。

### 4.3 Skill Catalog Backend

`SkillCatalogBackend` 是必要的薄适配器，原因是：

- Skill 来源是公共内置目录；
- 每次请求只允许当前 App 绑定的 Skill 子集；
- 主 Agent 和 SubAgent 的 Skill 前缀不同；
- Web 服务不能把宿主机目录直接暴露给模型；
- Skill 目录必须只读。

它不应复制 `SkillsMiddleware` 的发现、解析或加载算法，只提供虚拟文件
访问。`SKILL.md` 的 frontmatter 由 `SkillService` 扫描时校验；
Backend 只负责对已校验包做只读投影。

### 4.4 Memory Backend

长期记忆的权威数据是 DeepAgents 文件记忆，不再维护 `memory_facts` 业务表。

运行时优先使用 DeepAgents 原生：

```python
StoreBackend(
    store=store,
    namespace=lambda runtime: scoped_namespace,
)
```

Store namespace 由 `AgentRunContext` 生成；没有配置 Store 时不声明长期记忆
能力，不能伪造跨线程持久化。

### 4.5 Sandbox Backend

`backends/sandbox.py` 不是第二套执行引擎，而是官方 Sandbox provider 的
生命周期适配器。它只负责：

1. 创建官方 Sandbox；
2. 上传已授权 Skill、Memory、Context 和 Workspace 初始文件；
3. 将官方 Sandbox Backend 作为 `CompositeBackend.default`；
4. 运行结束同步必要 Workspace 文件；
5. 删除 Sandbox 并关闭关联资源。

项目不注册 `run_skill_script`、`shell` 或其他自定义执行工具。Skill 脚本
由原生 `execute` 在官方 Sandbox 中执行。

## 5. CompositeBackend 的正确组合方式

### 5.1 非 Sandbox 模式

非 Sandbox 模式下，默认 Backend 的职责是承载 DeepAgents 当前线程内的
临时文件和框架内部文件。规划上应优先使用原生 `StateBackend` 作为 default，
而不是让 `DenyBackend` 冒充默认存储。

业务文件通过显式 route 接入：

```python
CompositeBackend(
    default=StateBackend(),
    routes={
        "/workspace/": workspace_backend,
        "/artifacts/": workspace_backend,
        "/skills/": skill_catalog_backend,
        "/subagent_skills/": subagent_skill_backend,
        "/memories/": memory_backend,
    },
)
```

这里有两个独立的安全边界：

- `permissions=` 决定模型使用原生文件工具时能否操作路径；
- 每个业务 Backend 自己决定数据是否存在、是否属于当前作用域以及是否可写。

未授权模型路径由文件权限最终拒绝；业务 Backend 不因为模型传入了路径就
接受跨租户查询。`DenyBackend` 仍然有价值，但它应作为显式拒绝路由或特殊
场景 Backend，不应默认替代 DeepAgents 的线程内状态 Backend。

### 5.2 Sandbox 模式

Sandbox 模式下，官方 Sandbox Backend 必须作为 default：

```python
CompositeBackend(
    default=official_sandbox_backend,
    routes={},
)
```

原因是 DeepAgents 的 `execute` 不按虚拟路径路由，始终交给 default Backend。
只有官方 Sandbox 作为 default，文件工具和 `execute` 才会访问同一个隔离文件
系统。

Skill、Memory、Context 和 Workspace 的初始文件在创建 Sandbox 时上传。
运行结束只把允许回收的 Workspace 路径同步回 PostgreSQL，Sandbox 内部的
临时文件、Skill 包和 Memory 副本随 Sandbox 删除。

### 5.3 StoreBackend 的路由

Memory 使用 StoreBackend 时，必须同时提供 LangGraph Store，并保证 namespace
由服务端作用域生成：

```text
("mydify", account_id, app_id, end_user_id_or_account)
```

namespace 不能来自模型输入，也不能由 Skill、SubAgent 或请求文件覆盖。

## 6. 权限分层

### P1：能力可见性

- `skills=` 只传当前已授权 Skill 路径；
- SubAgent 的 `skills=` 只传主 Agent 已授权集合的子集；
- `execute` 只在 Sandbox default 能力存在时出现；
- 业务工具仍由应用配置和平台工具注册表决定。

### P2：DeepAgents 文件权限

`permissions=` 只保护原生文件工具。非 Sandbox 模式必须显式配置允许路径
和最后的全局拒绝；Sandbox 模式遵循当前 DeepAgents 版本对可执行 Backend 的
约束，不额外伪造 execute 权限层。

### P3：Backend 业务 ACL

Backend 每次操作都必须使用构造时绑定的服务端作用域，不接受模型传入的
account/app/conversation 身份。只读 Backend 的 `write/edit/upload` 必须
自身拒绝，不能只依赖 `permissions=`。

### P4：Sandbox 隔离

Sandbox provider 负责进程、文件系统、网络和资源限制。项目只传递已授权文件
和 provider 配置，不实现宿主机 Shell fallback。

## 7. 重构落地状态

本轮已完成 Backend 结构和运行时组合调整：

1. `SkillCatalogBackend` 位于 `backends/skill_catalog.py`。
2. 长期记忆由 `runtime/memory.py` 装配原生 `StoreBackend`，不再有
   `MemoryProfileBackend`。
3. `runtime/assembly.py` 只保留投影、校验和请求级路由装配。
4. CompositeBackend 集中由 `backends/composite.py` 组装。
5. 非 Sandbox default 使用原生 `StateBackend`，不让拒绝型 Backend
   承担线程内临时文件存储。
6. Sandbox default 仍使用官方 Sandbox，未增加自定义执行工具分支。
7. `AgentContextAssembler` 只合并投影路由并接收最终 Backend 资源。
8. 测试文件保持不变，后续测试调整等待用户明确要求。

## 8. 暂不做的事情

- 不实现第二套 FilesystemMiddleware；
- 不实现第二套 SkillsMiddleware；
- 不实现自定义 Shell 或 Skill Script Runner；
- 不把 `SKILL.md` 内容拼进 system prompt；
- 不把 `WorkspaceRepository` 直接暴露给 Agent；
- 不在没有 Store 时伪造跨线程长期记忆；
- 不为了“默认拒绝”破坏 DeepAgents 内部临时文件和摘要文件流程；
- 本轮不修改测试文件。
