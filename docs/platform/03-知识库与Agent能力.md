# 知识库、工具、MCP、工作流、Skill、Workspace 和长期记忆


本文档按业务域拆分，统一描述业务需求、业务运转流程、数据库设计、代码实现和面试讲解重点。总目录见 [平台文档目录](00-平台总览.md)。

具体实现见 [知识库](implementation/06-知识库索引与检索.md)、[能力集成](implementation/07-工具MCP工作流与Skill.md)、[文件交付](implementation/08-工作区文件交付与执行环境.md) 和 [按需记忆](implementation/05-提示词上下文压缩与记忆.md)。


## 5. 模块四：知识库与 RAG

### 5.1 业务流程

1. 创建 Dataset，选择 Embedding Provider、模型和维度。
2. 上传文件，生成 UploadFile 和 Document，选择解析/切分规则。
3. 异步执行“解析 → 切分 → 向量化 → 写入 Weaviate”，更新文档和片段状态。
4. 文档完成后启用；失败保留错误和阶段时间，支持重试或删除。
5. 应用配置绑定 Dataset 和检索参数。
6. Agent 运行时对问题做向量检索，记录 DatasetQuery，返回片段作为上下文。

### 5.2 数据库设计

| 表 | 关键字段 | 作用 |
|---|---|---|
| `dataset` | `account_id,embedding_provider,embedding_model,embedding_fingerprint,embedding_dimension` | 知识库和向量模型元数据 |
| `document` | `dataset_id,upload_file_id,process_rule_id,batch,status,各阶段时间,error` | 文档处理任务 |
| `segment` | `dataset_id,document_id,node_id,content,keywords,hash,status,hit_count` | 可检索文本片段与向量节点映射 |
| `process_rule` | `dataset_id,mode,rule` | 解析和切分规则 |
| `dataset_query` | `dataset_id,query,source,source_app_id,created_by` | 检索审计和统计 |
| `keyword_table` | `dataset_id,keyword_table` | 关键词索引辅助数据 |
| `upload_file` | `account_id,key,size,extension,mime_type,hash` | 原始文件对象元数据 |

关系库保存业务元数据，Weaviate 保存向量；`segment.node_id` 是跨存储关联键。删除 Dataset 时需先解除 App 绑定，再删除向量节点和关系记录。Embedding 指纹变化时禁止直接混写，建议新建索引批次并切换版本。

### 5.3 代码实现

- Service：`internal/service/dataset`、`internal/service/file`。
- Model：`internal/model/dataset.py`、`upload_file.py`。
- 前端：`src/views/space/datasets`、`src/hooks/use-dataset.ts`。

## 6. 模块五：工具、MCP、工作流与 Skill

### 6.1 业务流程

1. 账号创建或从商店安装能力资源。
2. API Tool 以简化 OpenAPI Schema 描述外部接口；MCP Server 连接后发现 MCP Tool；Workflow 保存可执行图；Skill 保存版本化指令/资源。
3. 应用草稿绑定资源，保存时校验资源属于当前账号且处于可用状态。
4. 发布时把绑定快照冻结到 App Config；运行时由 AgentFactory 解析并注入工具、子 Agent 和中间件。
5. 工具调用记录到事件流，失败作为工具事件和最终错误的一部分返回。

### 6.2 数据库设计

| 能力 | 表 |
|---|---|
| API Tool | `api_tool_provider`、`api_tool` |
| MCP | `mcp_server`、`mcp_tool` |
| Workflow | `workflow`、`workflow_result` |
| Skill | 代码内置 Skill 目录及应用配置中的 `skills` 快照 |
| 模型连接 | `provider_api_key` |

工具配置中的 Header、环境变量和密钥应加密存储；MCP 工具发现结果需要版本/发现时间；Workflow 发布前应保存调试通过标志。资源删除前检查 App Config 引用，已发布配置引用的资源采用禁用或软删除。

### 6.3 代码实现

- Service：`internal/service/tool`、`workflow`、`skill`、`language_model`。
- Model：`internal/model/api_tool.py`、`mcp_server.py`、`workflow.py`。
- 前端：`src/views/space/tools`、`workflows`、`store`。

## 7. 模块六：Workspace 文件与长期记忆

### 7.1 业务流程

1. 持久化文件按 Account + App 生成稳定 scope，每轮创建独立工作副本。
2. Agent 普通文件工具读写副本；通过 list_saved_files/load_saved_file 按需访问历史文件。
3. deliver_file 显式交付选定结果，元数据入库、正文进对象存储，并通过 revision 防止并发覆盖。
4. 长期记忆另由 LangGraph Store 保存，通过 /memories/ 按需读写；普通问答不自动读取或总结写入。
5. 文件预览与下载重新校验作用域和访问权限，拿到路径不代表有权访问。

### 7.2 数据库设计和实现

`workspace_files`：`account_id`、`app_id`、`conversation_id`（当前代码中是 scope 标识）、`path`、`object_key`、`mime_type`、`size_bytes`、`revision`、`created_by`、`is_deleted`。对象内容与元数据分离，删除先提交软删除状态，再尽力清理对象；当前不是独立异步垃圾回收队列。代码位于 `internal/model/workspace_file.py`、`internal/service/file`、`internal/repository`，前端位于 `WorkspaceFilesPanel.vue`。

建议把 scope 类型显式化（app/shared/conversation），避免使用名为 `conversation_id` 的派生 scope 字段造成误解；所有下载接口先做应用、账号、WebApp Token 和会话校验。



