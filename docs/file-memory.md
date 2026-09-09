# 文件式长期记忆

编排页只负责 `long_term_memory.enable` 开关。开启后，工作台开放长期记忆文件入口，根 Agent 获得文件记忆能力；关闭后隐藏入口并停止读写，现有数据保留。

## 用户操作

- 用户偏好、背景信息、经验方法分别对应 `/memories/preferences/`、`/memories/background/`、`/memories/experience/`。
- 每个文件一个主题；文件列表显示名称、摘要和更新时间。
- 新建和编辑直接进入文档编辑器，标题即文件名，正文自动保存。
- 修改分类或标题通过一次原子请求移动文件，不先复制再删除。
- 删除直接移除 Store 文件，不提供候选、暂停、版本历史、撤销或回收站。
- 自动保存失败保留草稿，支持重试或明确放弃修改；关闭面板前等待保存完成。

## 框架装配

数据沿用 LangGraph `PostgresStore` 和 DeepAgents `StoreBackend(file_format="v2")`，不新增业务表。测试使用 `InMemoryStore`。

namespace 仍为 `("mydify", "agent-memory", account_id, app_id)`。WebApp 的 account_id 来自当前登录使用者，创建者入口来自当前创建者；身份由服务端确定。

`ReloadingMemoryMiddleware` 继承原生 `MemoryMiddleware`，保留原生学习提示和系统消息注入，每次模型请求刷新记忆目录。`create_deep_agent(memory=None)` 避免重复注册默认中间件。

`/memories/.index.md` 是 Backend 动态生成的只读目录，不占用用户文件，也不出现在工作台。索引和自动加载的完整短偏好合计不超过 3,600 UTF-8 字节；其他正文通过原生 `read_file` 按需读取。超过目录预算的文件可通过 `ls` 查找。

Agent 使用原生 `write_file` 和 `edit_file` 新建、纠正记忆；仅删除操作补充 `delete_memory_file`。文件工具成功回执经现有工具事件投影为“记忆已更新”。不增加独立总结模型调用。

## 一致性

HTTP 写入与 Agent 文件操作共享作用域事务锁。工作台传递当前正文的 SHA-256 摘要，拒绝覆盖其他会话的新修改；空摘要表示仅允许新建。摘要不构成版本历史。PostgreSQL 锁跨进程生效，内存测试使用进程锁。

单文件 64 KiB、作用域最多 100 个文件、总容量 1 MiB。内部索引不可编辑。Agent 写入沿用原生“新建不能覆盖、编辑须精确匹配”的语义。

## 旧数据

首次访问文件目录时，在同一作用域事务中将旧结构化目录的 active 条目转成分类文件，并保留内容中的适用范围。候选、暂停、遗忘和修订历史不转成文件。成功后删除旧 catalog 并写入独立导入标记，避免用户删除文件后重新导入。

导入遇到同名文件时追加数字后缀，两份内容均保留。旧数据导入不受新建配额截断；若合并后超限，仍能查看和删除，整理到容量限制内后可继续保存。

旧 MEMORY.md 沿用原先的字面主题和历史内容过滤，排除已管理信息；其余旧文件仍可在背景信息分类查看、编辑和移动。字面过滤不提供语义冲突识别，旧自由文本仍可能需要用户整理。

实现未直接执行业务数据库修改；数据转换发生在部署后首次使用时，不需要数据库结构迁移。

## 验证

后端：`.venv/Scripts/python.exe -m pytest tests/test_file_memory.py tests/test_context_window.py tests/test_agent_user_input.py -q`

前端：`npx vitest run src/components/MemoryFilesPanel.test.ts src/utils/memory-updates.test.ts`，`npm run build`。

真实 DeepAgents 同步和异步工具循环使用本地测试模型与内存 Store；浏览器检查使用模拟 HTTP 数据，不调用业务模型或修改真实用户记忆。
