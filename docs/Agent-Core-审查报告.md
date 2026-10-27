# Agent Core（internal/core/agent_v2）审查报告

- 审查对象：`internal/core/agent_v2/`，72 个 .py / 7233 行（未含 2 份 README）
- 审查方式：整目录审查（该目录相对 `HEAD` 无改动，无可审 diff）
- 双轴：**契约**（`agent_v2/README.md` + `docs/Agent-业务文档.md`）与**规范**（`my-dify/AGENTS.md`）
- 最后被动过的提交：`24c6229 refactor(agent): reshape the run model and drop cross-process leasing`
- 证据分级：
  - **[实证]** 执行代码或读框架源码验证得出的结论
  - **[复核]** 子代理报告，已复读对应代码确认
  - **[存疑]** 证据不足、误报或需额外信息才能定论（附理由）
- **修复状态**：P0 三条已全部修复（§8）。初版有 2 条 P0 判断经进一步取证后**更正**（§9），其中一条原判为代码 bug，实为注释措辞错误。

---

## 0. 摘要

| 级别 | 数量 | 内容 |
| --- | --- | --- |
| P0 高危 | 3 | 子 Agent Shell 权限绕过（已修）、记忆校验注释与契约不符（已修）、连接池构造期泄漏（已修） |
| P1 中高 | 4 | 异常契约边界、沙箱重置顺序、消息结束分片缺失、用量口径分叉 |
| P2 中 | 6 | 记忆删除无事务、契约漂移、沙箱复用语义、路径语义硬编码、重复计算 |
| P3 规范 | 8 | README 漂移、注释与实现不符、坏味道 |

改动文件 3 个、4 处（§8）。另有 8 项**排除性验证**（§5）说明这些地方无需再查。

---

## 1. P0 高危（已修复）

### 1.1 通用子 Agent 保留 `execute`，可绕过文件权限 [实证]

**位置**：`assembly/graph/deep_agent_factory.py:54-64`、`middleware/shell_execution_middleware.py:18,20,62`

**证据链**（逐环验证）：

1. `shell_execution_middleware.py:18` 声明「文件权限不约束 Shell；**在模型与执行入口同时限制子 Agent 的共享执行能力**」，`:62` 再次声明「文件权限无法约束任意命令」。
2. 自定义子 Agent 照做了 → `subagent_spec_adapter.py:119` 显式传 `ShellExecutionMiddleware(allow_execution=False)`。
3. **漏洞面比初版判断更宽**，涉及两条路径：
   - **路径 A（我们注入的 GP）**：`deep_agent_factory.py:57-61` 注入的 spec 只传 `virtual_paths=True`，`allow_execution` 走默认 `True`（`:20`）。
   - **路径 B（框架自动注入的 GP）**：框架 `deepagents/graph.py:717-770` 在调用方未提供同名 spec 时**自己会注入** GP，其 `tools` 取 `_tools or []`（`:749`，即全部父工具），`middleware` 为一组基础中间件（`:718-727`），**完全不含 Shell 限制**。
4. `execute` 的来源是 `FilesystemMiddleware`（`deepagents/middleware/filesystem.py:1691`），而框架对**每个** subagent 都会前置该中间件（`graph.py:643-652`），触发条件是 `supports_execution(backend)`；对 `CompositeBackend` 只看 `default`（`filesystem.py:668-669`）。因此**有沙箱时每个 subagent 都会拿到 `execute`**。
5. 初版只覆盖路径 A，是因为误以为 `if runtime.execution_uses_virtual_paths` 之外由框架兜底即安全。实际上 `execution_uses_virtual_paths` 为 False 时（langsmith、bubblewrap）走的正是路径 B，**那条路径连 Shell 中间件都没有**。
6. 旁证：`deepagents/profiles/harness/harness_profiles.py:538` 出现的 `excluded_tools={"execute"}` 只是文档示例，不是内建 profile；项目也未注册任何 HarnessProfile（全仓检索无匹配），所以不存在现成缓解。

**影响**：通用子 Agent 同时具备 `execute` 与放行状态，可用 Shell 写入主 Agent 被禁止写入的 `/skills/**`、`/subagent_skills/**`——而这两个目录正是 `sandbox_skill_upload.py:41-50` 用 `chmod -R a-w` 刻意保护的只读授权包。父权限定义见 `assembly/context/sandbox_context.py:13-56`。

**修复**：`deep_agent_factory.py` 去掉 `execution_uses_virtual_paths` 外层条件，**两种模式都显式声明** GP spec，并传 `allow_execution=False`。框架的「调用方已提供同名 spec 则不再自动注入」逻辑（`graph.py:717`）保证我们的 spec 生效。

行为等价性已核对：框架对调用方提供的 spec 同样补基础中间件与 prompt caching（`graph.py:643-663`）、同样按 `spec.get("permissions", permissions)` 继承父权限（`:640`）、tools 同样经 `_apply_tool_description_overrides` 处理（`:695`，空 override 时为浅拷贝，不改顺序与身份，见 `deepagents/_tools.py:29-65`）。项目未注册 HarnessProfile，故无 prompt/description 覆盖差异。

### 1.2 记忆并发校验：注释与已文档化的契约不符 [实证，已更正]

> **本条推翻了报告初版的判断。** 初版判为「`None` 短路导致并发编辑静默丢失更新（数据丢失 bug）」，**该结论错误**：代码是对的，错的是注释。

取证：

- `docs/Agent-API文档.md:2627`：该接口用于创建或更新长期记忆文本文件……写入采用乐观并发控制：**传入** `expected_sha256` 时，若文件当前摘要不匹配则拒绝覆盖。
- `:2638`：`expected_sha256` 为**可选参数**。
- `internal/schema/memory/memory_file_schema.py:30-32`：`validators=[Optional(), Regexp(...)]`。
- `agent_memory_repository.py:185-186`：`if expected is None: return`。

即 **API 文档、Schema、代码三者一致**（可选乐观锁，不传即普通覆盖），只有 `_check_expected` 那句 docstring「空摘要表示只允许新建」与三者相悖。旁证：同一 helper 也服务 `delete_file`（`:151`），而删除操作不存在「只允许新建」的语义。

模型侧写入路径另由 `memory_file_transaction` 的 advisory lock 保护（`agent_memory_backend.py:66-107`），不依赖该字段。

**修复**：只更正 docstring 措辞（1 行），不改逻辑。

### 1.3 连接池构造期泄漏 [实证，范围已更正]

> **范围更正**：初版同时指出 `langgraph_store_provider.py:69`，**该处不成立**。`PostgresStore.from_conn_string` 是 `@contextmanager`，内部为 `with ConnectionPool(...) as pool: yield ...`（`langgraph/store/postgres/base.py:795-796`），因此 `__enter__` 内构造失败或后续 `setup()` 失败都会被该 `with` 关闭池。仅 checkpoint 侧是真泄漏。

**位置**：`persistence/langgraph_checkpoint_provider.py:147-158`（原 `:152`）

```python
pool = connection_pool_factory(conninfo=connection_string, **pool_config, open=True)  # :147
saver = saver_factory(pool)      # :152  ← 原在 try 之外；池由调用方显式持有，无 with 兜底
try:
    saver.setup()                # :154
except Exception:
    close_connection_pool(pool)  # :156  只覆盖 setup() 失败
    raise
```

若 `saver_factory(pool)` 自身抛错，`:147` 已建立的**连接池与 worker 线程永不关闭**。

**修复**：把 `saver_factory(pool)` 移入 try，与 `setup()` 共用同一条归还路径。

---

## 2. P1 中高

### 2.1 `except Exception` 与 `BaseExceptionGroup` 的契约边界 [实证]

> **本节修正了子代理的原始结论。** 子代理报告为「清理错误**永远**拿不到 `failed`，契约全部不成立」。用真实对象实测后确认该表述**过强**，实际是条件性缺陷。

`runtime_resources.py:66,94` 用 `BaseExceptionGroup(...)` 构造清理错误，`agent_executor.py:99,107,289` 用 `except Exception` 接。实测类型塌缩行为（Python 3.13，PEP 654）：

| 收集到的错误 | 构造结果类型 | `except Exception` 能否捕获 |
| --- | --- | --- |
| 全部是 `Exception` 子类 | 自动塌缩为 **`ExceptionGroup`** | **能**（契约成立） |
| 含任一非 `Exception` 基异常 | 保持 `BaseExceptionGroup` | **不能** → 逃出 `execute()` |
| `close_runtime_backends` 的组再被包一层（全为 Exception） | `ExceptionGroup` | **能** |

**真实缺陷**：仅当某个 `close()` 抛出非 `Exception` 的 `BaseException`（如 `asyncio.CancelledError`、`GeneratorExit`）时，组才会穿透 `except Exception` 逃出 `execute()`，使 `delivery.finish()`（`:113`）与 `_build_result()`（`:120`）被跳过，服务层只等到裸异常，Run 停在 `running` 直到孤儿收敛。

**为何不是纯理论**：同文件 `:209` 自己就用了 `except BaseException`；且 4 个沙箱 provider 都写了同一句防护——`bubblewrap_sandbox_backend.py:332`、`daytona_sandbox_backend.py:369`、`langsmith_sandbox_backend.py:70`、`local_shell_backend.py:92`：

```python
if not isinstance(exc, Exception):
    raise
```

说明本仓库明确预期会出现非 `Exception` 的基异常。**异常类型边界在同一模块内自相矛盾。**

**建议**（未修，属 P1）：`agent_executor.py:99/107/289` 改为 `except BaseException`，或让 `runtime_resources` 只收集 `Exception` 并把基异常直接 `raise`。

### 2.2 LangSmith 复用沙箱时「保护」与「重置」顺序冲突 [存疑→倾向成立]

`langsmith_sandbox_backend.py:59` 上传 Skill 时 `protect=True`，实现是 `chmod -R a-w -- skills subagent_skills`（`sandbox_skill_upload.py:41-50`）。复用同一沙箱时 `langsmith_sandbox_backend.py:100-104` 直接：

```python
"find /workspace -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + && "
"rm -rf -- /skills /subagent_skills"
```

**没有**先 `chmod -R u+w`，而 daytona 的同类实现**有**（`daytona_sandbox_backend.py:425` 附近）。若沙箱运行用户非 root，`rm -rf` 删除 `a-w` 目录内条目会被拒，`:106-107` 随即抛 `SandboxProviderError`。

**[存疑理由]** 取决于 LangSmith 沙箱的执行用户是否为 root，无法从本仓库确认。「同一形状两种实现」本身已构成维护风险。

### 2.3 已交付文本可能没有结束分片 [复核]

`README.md:107` 承诺「无 finish 分片、异常或取消退出时，只补齐**已经交付**的文本」。但 `event_normalizer.py:153-154`：

```python
if event == "message-start":
    self._text_streams[key] = _TextStream(node=node)   # ← 直接覆盖，旧流的 kind/text 被丢弃
```

同一 `key = (namespace, node)`（`:151`）在未收到 `message-finish` 时再次 `message-start`，旧流被静默丢弃，`finish()`（`:87`）无从补齐——客户端会看到永远等不到 `message-finish` 的消息。

**[存疑理由]** 触发需「同一 (namespace,node) 连续两次 message-start 且中间无 finish」，未确认框架是否产生该序列。事件缓冲层有兜底（`agent_workbench_event_buffer.py:73`），可缓解不可替代。

### 2.4 用量解析口径分叉 [复核]

同一供应商字段，两套策略：

- `observability/token_usage.py:118-123` `_is_token_count`：**只接受** `int/float`，字符串数字被判为「字段缺失」→ 整段用量丢失并记成「未返回用量」。
- `middleware/context_window_middleware.py:121-126`：`type(value) is int` **或** `isinstance(value, str) and value.isascii() and value.isdecimal()` → 接受十进制字符串。

建议把解析收敛到 `token_usage`。

---

## 3. P2 中

### 3.1 `delete_app_memories` 未进事务 [复核]

`agent_memory_repository.py:157-180` 未走 `memory_file_transaction`，`:178` 逐条 `store.delete(...)` 自动提交。无锁无事务，中途失败留半删除状态；并发写入可让已删条目复活。

### 3.2 沙箱同名复用语义不成立 [复核]

`daytona_sandbox_backend.py:340-343` 构造 `CreateSandboxFromSnapshotParams` 时**未传 `name`**，name 只作本地池键（`:345-349`）；langsmith 的 `_langsmith_create_kwargs`（`:79-91`）确实传了 `name`。同一契约在两种 provider 下行为不同。

### 3.3 `execution_uses_virtual_paths` 硬编码单一 provider [部分误报]

`lazy_sandbox_backend.py:52`：`self.execution_uses_virtual_paths = provider == "daytona"`。

- **langsmith 判 False 是正确的** —— `langsmith_sandbox_backend.py:100-104` 直接操作 `/workspace`、`/skills`，说明沙箱内就是真实绝对路径。子代理将本条列为缺陷，判为**误报**。
- **bubblewrap 需确认** —— `bubblewrap_sandbox_backend.py:227-253` 挂载 `/workspace`，未逐行确认是否与虚拟路径一一对应。
- 真正的问题是**形状**：用 provider 字符串推断能力，散落在 `lazy:52`、`sandbox_backend_factory.py:140-147`、`_translate_virtual_path`，新增 provider 需同步改多处（Primitive Obsession / Shotgun Surgery）。

### 3.4 `user_input_enabled and checkpointer` 重复计算 [复核]

`deep_agent_factory.py:40`（决定提示词是否教模型调 `ask_user`）与 `platform_middleware.py:93`（决定是否挂 `UserInputMiddleware`）各算一次。漂移即出现「提示词教模型调 `ask_user`，但中间件没挂」。`runtime_resources.py:41` 已有 `execution_uses_virtual_paths` 属性这一先例，建议同法上移。

### 3.5 README 固定链漏列 Shell 中间件 [复核]

`README.md:76` 声明的固定链未含 Shell，但 `platform_middleware.py:57` 在「工具消息校验」与「Runtime Context」之间插入了 `ShellExecutionMiddleware`（业务文档 §5.4 已正确列出）。**按 README 读会误判顺序。**

### 3.6 其它契约与注释不符（同类合并）

| 位置 | 声明 | 实现 |
| --- | --- | --- |
| `runtime_assembler.py:145` | README:43「装配器只维护**一份**待清理 Backend 列表」 | `owned_backends = [backend]` 覆盖列表；先前登记的条目仅因 `close_runtime_backends` 展开 `CompositeBackend.default/routes` 才被释放 |
| `subagent_spec_adapter.py:114` | 注释「显式空列表避免继承父工具」 | `"tools": tools` 是已授权工具，通常非空 |
| `skill_context.py:140` | 注释「避免路由覆盖和路径逃逸」 | 实际只校验 Skill 名；`subagent_config.name` 同样拼进路由前缀却未校验 |
| `evaluation_evidence.py:18-21` | 与 `message_parser.final_answer:54` 语义分叉 | 前者不按 human 边界截断、只认 `AIMessage` 实例（字典消息静默得 `answer=""`） |
| `event_normalizer.py:165` | 同函数 `:162,:168` 用 `or {}` | `data["index"]` 直接下标，缺 index 会 KeyError 使整轮 `failed` |

> 关于 `runtime_assembler.py:145`：已核实**中间的 `append` 并非死代码**——它在「sandbox 构造成功但 CompositeBackend 构造失败」等提前失败路径上承担清理职责。它只是与 README 的「一份列表」表述不符，不是缺陷，建议改的是 README 措辞。

### 3.7 其它 provider 差异（子代理报告，未逐条复核）

- `daytona_sandbox_backend.py:342` `auto_delete_interval=0` + 进程内池 + `:387-389` 只 `invalidate` 本进程 → 多 worker / 重启 / SIGKILL 后远程实例无人删除。
- `langsmith` 无租约互斥（`langsmith:44-59`），daytona 有（`session_sandbox_pool.py:56`）。
- `agent_memory_backend.py:193` 配额估算只 `store.search(..., limit=max_files+1)`，超量时 `total_bytes` 偏小。
- `bubblewrap:330-331`、`daytona:365-366` 的 `except Exception: pass` 静默吞掉清理异常。

---

## 4. P3 规范与坏味道

### 规范（`AGENTS.md`）

| 位置 | 问题 |
| --- | --- |
| `postgres_connection.py:74-76` | `except (...): raise` 纯转发，不属异常转换/重试/补偿/清理 |
| `postgres_connection.py:78-83` | 只记 `type(exc).__name__`，未用 `logger.exception`，重试再失败即丢原始错误 |
| `bubblewrap:330-331`、`daytona:365-366` | `except Exception: pass` 静默吞异常 |
| `sandbox_backend_factory.py:122-129` | `logger.warning(..., exc_info=True)` 应为 `logger.exception` |
| `agent_executor.py:103,111,119,292` | 四条 `logger.exception` 均无 run_id，缺排障上下文（对比 `summary_progress.py:134` 带 `call_id`） |
| `summary_progress.py:87-89,131-134` | 静默兜底，越出四种允许用途 |
| `event_normalizer.py:310-316,326-329` | `model_dump` 失败静默降级 `str(value)`，掩盖序列化错误（该函数是 README:105 的 JSON 出口） |
| `entity/user_input.py:166` | `except (KeyError, TypeError, ValueError): continue` 静默吞异常，无日志 |
| 缺步骤注释 | `agent_memory_repository.py:94-132`、`user_input.py:156-190`、`evidence_rubric_middleware.py:92-139`、`context_window_middleware.py:52-105`、`build_skill_context` |
| 方法顺序 | `sandbox_backend_factory.py:78-97` 私有工具方法夹在两个公开入口之间 |
| 注释长度 | `lazy_sandbox_backend.py:31-34` 类注释 4 行，超出 1–3 行 |

### 坏味道（Fowler 基线）

**Duplicated Code**
- `except BaseException → 清理 → 非 Exception 原样抛 → 包 SandboxProviderError` 四份：`bubblewrap:325`、`daytona:360`、`langsmith:61`、`local_shell:90`。
- `len(responses) != len(files)` 四处：`sandbox_initialization.py:23`、`sandbox_skill_upload.py:38,110,123`。
- 「先 allow 再 `/**` 兜底」权限骨架两份：`subagent_spec_adapter.py:164-212`、`sandbox_context.py:13-57`。
- `parent = super()` + lambda 样板在 `langgraph_checkpoint_provider.py:49-127` 重复 8 次。

**Speculative Generality**（全仓库无调用方传值）
- `langgraph_checkpoint_provider.py:197` / `langgraph_store_provider.py:110` 的 `mode` 参数；
- `langgraph_checkpoint_provider.py:171-174`、`langgraph_store_provider.py:82-84` 的 5 个工厂注入参数；
- `subagent_spec_adapter.py:71,75` 的 `skill_sources_by_agent=None` + `or {}`（唯一调用点恒传值）。

**Divergent Change**
- `event_normalizer.py` 单文件承担信封校验、通道分发、文本分片状态机、JSON 序列化、namespace 解析（`:20-27 / 49-85 / 102-249 / 307-341`）。

**Primitive Obsession / Repeated Switches**
- provider 字符串分支散在 `lazy:52`、`factory:24-25,140-147`。
- 虚拟路径字面量散在 `skill_context.py:84,105`、`runtime_assembler.py:134,141`、`sandbox_context.py:16-41`、`subagent_spec_adapter.py:186-204`。
- 工具名裸串硬编码于 `platform_middleware.py:38`、`evaluation_tool_middleware.py:9-11`，而 `entity/tool.py:3` 才是注册处。
- 模式字符串分支三处重复：`langgraph_checkpoint_provider.py:200-212`、`langgraph_store_provider.py:110-122`、`config/config.py:250,265`。

**Feature Envy / Message Chains**
- `agent_memory_backend.py:182` 用 `namespace[-2]/[-1]` 反推身份构造 `AgentMemoryIdentity`。
- `runtime.skill_projection.sources` 被 `deep_agent_factory.py:51,59,70`、`platform_middleware.py:53` 穿透三层取用。
- `memory_file_transaction.py:26-31` 直读 `store.conn` 与 `connection.connection()`，同包 `langgraph_checkpoint_provider.py:228` 却用 `getattr` 鸭子类型——同一层两种风格。

**Mysterious Name**
- `workspace_delivery.py:51-52` 与 `:74-75` 的 `source/target` 含义相反；`:64` 写 `(target,source)`、`:83` 读 `(source,target)`。
- `workspace_scope.py:22` 名称尾段重复 `account_id`。

**其它**
- `evaluation_tool_middleware.py:20` 用 `getattr(tool,"name")` 过滤，`shell_execution_middleware.py:49` 同场景兼容 dict → dict 形态工具会被整批过滤。

---

## 5. 排除性验证（无需再查）

1. **无循环依赖** —— AST 解析全目录构建 import 图，13 个包/模块组之间无环，分层是干净 DAG。
2. **`agent_executor` 的 seq 起点无误报** —— `langgraph/stream/_mux.py:105,294` 确认框架 `seq` 从 1 开始，故 `_last_raw_sequence = 0` 正确。
3. **链式异常 `from` 未丢** —— `agent_executor.py:103,111,119,292` 在 error 已存在时用 `logger.exception` 保留堆栈。
4. **CAS 语义本身成立** —— `memory_file_transaction` 同事务 + `pg_advisory_xact_lock`，借还配对、异常路径会归还。
5. **`run_observer` 无竞态** —— 累加、活动模型 ID 更新、重复结束去重同在一把 `RLock`（`:47,87-94,107-111`），结束后拒绝迟到回调。
6. **权限规则匹配顺序正确** —— `build_filesystem_permissions` 的「先 allow、末尾 `/**` 兜底」与框架 `_check_fs_permission`（`filesystem.py:127-137`）的「首个匹配生效」一致；`sandbox` 分支的 `"/": sandbox` 路由是框架 `_all_paths_scoped_to_routes` 硬要求。
7. **store provider 的 `__enter__` 不泄漏** —— `from_conn_string` 是 `@contextmanager`，内部 `with ConnectionPool(...) as pool`（`base.py:795-796`），构造失败由该 `with` 关闭（本项更正了初版判断，见 §9）。
8. **框架 tools 传递路径等价** —— `_apply_tool_description_overrides`（`deepagents/_tools.py:29-65`）在 override 为空时仅浅拷贝列表，不改变顺序与工具身份，故「框架自动注入 GP」与「我们显式注入 GP」两条路径的 tools 完全等价（本项用于支撑 §1.1 修复的行为等价性）。

另有一项**已驳回**的判断：子代理称「框架权限可被 `..` 绕过」（实测 `_check_fs_permission` 对 `/workspace/../../../etc/passwd` 返回 `allow`，一度倾向成立）。进一步核实后**不成立**：框架文件工具在派发前统一调用 `validate_path`（`deepagents/backends/utils.py:502` 拒绝含 `..` 的路径），见 `filesystem.py:1160,1187,1226,1265` 等 12 处调用点。`_translate_virtual_path` 放行未映射路径是**有意设计**（daytona 需要 `/tmp`），且文件工具已被 `validate_path` 覆盖。**真正的风险是 §1.1 的 `execute` 不受任何路径校验约束**，而非 `..` 穿越。

---

## 6. 已知问题现状（不重复展开）

| 既有记录项 | 状态 |
| --- | --- |
| `deep_agent_factory.py` 工具保留名校验在追加交付工具之前 | **仍存在** |
| `ReviewMiddleware` 在图与 `AgentExecutor.__init__` 各构造一份 | **仍存在** |
| `agent_executor.py:206` 提问中断 namespace 硬编码 `[]` | **仍存在** |
| `runtime_resources.py:48-49` `close()` 持续抛已保存异常 | **仍存在**（叠加 §2.1 形成边界矛盾） |
| `entity/run_context.py` `principal_id` 死字段 | **仍存在** |
| 业务文档 §6.9 声明窗口观测 7 类，实现只产出 6 类（`summary` 恒不出现） | **仍存在** |

---

## 7. 剩余建议顺序

1. **§2.1 异常边界统一** —— 三处 `except Exception` → `except BaseException`，并同步 README 措辞（与已修的 P0 同属资源清理路径，建议紧接处理）。
2. **§2.2 沙箱重置顺序** —— 需先确认 LangSmith 沙箱执行用户，再决定是否补 `chmod -R u+w`。
3. **§2.3 / §2.4** —— 分片收尾与用量口径，改动小、可独立验证。
4. **§3 / §4** —— 按模块就近修复；README/注释漂移建议成批更正，避免逐条改产生新的不一致。

---

## 8. 本轮修复记录

改动 3 个文件、4 处，全部经行为验证。

### `assembly/graph/deep_agent_factory.py`

去掉 `execution_uses_virtual_paths` 外层条件，两种模式都显式声明通用子 Agent，并传入 `allow_execution=False`：

```python
# 框架默认通用子 Agent 不继承主 Agent 中间件，却会继承父工具（含 execute）；
# 这里统一显式声明，按平台策略在执行入口关闭子 Agent 的共享 Shell。
if not any(spec["name"] == GENERAL_PURPOSE_SUBAGENT["name"] for spec in subagent_specs):
    subagent_specs.insert(0, {
        **GENERAL_PURPOSE_SUBAGENT,
        "skills": list(runtime.skill_projection.sources),
        "middleware": [ShellExecutionMiddleware(
            allow_execution=False,
            virtual_paths=runtime.execution_uses_virtual_paths,
        )],
    })
```

### `persistence/agent_memory_repository.py`

只改 docstring，逻辑不动：

```python
- """空摘要表示只允许新建，非空摘要防止自动保存覆盖已变更文件。"""
+ """可选乐观锁：未传摘要按普通覆盖处理，传入摘要则拒绝覆盖已变更或已删除的文件。"""
```

### `persistence/langgraph_checkpoint_provider.py`

把 `saver_factory(pool)` 移入 try，与 `setup()` 共用归还路径：

```python
try:
    # 工厂拒绝该 pool 时同样要归还连接池与 worker 线程。
    saver = saver_factory(pool)
    saver.setup()
except Exception:
    close_connection_pool(pool)
    raise
```

### 验证方式

- **`deep_agent_factory`**：桩掉 `create_deep_agent` 捕获实际传给框架的 `subagents`，在 `execution_uses_virtual_paths` 为 True/False 两种模式下断言通用子 Agent 的 `allow_execution is False`、`virtual_paths` 与运行模式一致，并断言自定义子 Agent 权限未变；另直接调用 `_denied_call` / `_prepare_request` 确认 `execute` 调用被拒、且不再暴露给模型。
- **`langgraph_checkpoint_provider`**：以假 pool/saver 覆盖三条路径，断言 `saver_factory` 抛错、`setup` 抛错时 `close()` 各被调用 1 次，成功时 0 次（池交给调用方长期持有）；并用旧写法做对照，确认旧写法在该场景下 `close()` 为 0 次（即真泄漏）。
- 三个文件 `py_compile` 通过；验证脚本为一次性临时文件，已删除。

---

## 9. 更正记录

初版报告有 2 条 P0 判断在进一步取证后更正，均记录在此以便追溯：

| 项 | 初版判断 | 更正后 | 触发更正的证据 |
| --- | --- | --- | --- |
| §1.2 记忆并发校验 | 「`None` 短路导致并发编辑静默丢失更新」，判为**代码的数据丢失 bug** | 代码**正确**，缺陷仅在 docstring 措辞 | `docs/Agent-API文档.md:2627,2638` 明确 `expected_sha256` 为**可选**乐观锁；Schema `Optional()` 与代码行为一致，docstring 是唯一异类；同一 helper 也服务 delete，不存在「只允许新建」语义 |
| §1.3 连接池泄漏 | checkpoint 与 store provider **两处**均泄漏 | 仅 checkpoint provider 泄漏；store 侧**不成立** | `from_conn_string` 为 `@contextmanager`，内部 `with ConnectionPool(...) as pool: yield ...`（`langgraph/store/postgres/base.py:795-796`），失败由该 `with` 关闭池 |
| §1.1 Shell 权限 | 仅「我们注入的 GP spec」缺 `allow_execution=False` | **范围更大**：框架 `graph.py:717-770` 也自动注入 GP，且 `graph.py:643-652` 对每个 subagent 前置 `FilesystemMiddleware`（`execute` 的来源），故非虚拟路径模式下同样受影响，且那条路径连 Shell 中间件都没有 | 读 `deepagents/graph.py` 与 `middleware/filesystem.py:653-672,1691` |

后两条的主因是首轮只核对了子代理报告的**结论**而非其**推理链**；§1.2 的主因是把代码注释当作契约，而仓库里真正权威的契约是 `docs/Agent-API文档.md`。

本文只做描述、分析与修复记录；除 §8 列出的 4 处改动外，未修改其它代码。
