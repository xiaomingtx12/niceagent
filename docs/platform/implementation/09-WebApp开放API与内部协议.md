# WebApp、开放 API 与内部协议

[返回模块目录](README.md)

## 入口不同，运行规则共用

| 入口 | 身份或凭证 | 配置来源 |
| --- | --- | --- |
| 平台调试/创建者任务 | 登录 Account | 按入口读取草稿或相应定义 |
| WebApp | 发布链接 Token，加应用范围的消费者/会话身份 | 发布配置 |
| 开放 API | API Key 绑定 Account | 发布应用 |
| 内部异步 Protocol | 绑定账号、父应用和目标应用的短期 HMAC Token | 目标应用发布配置 |

入口先解析可信主体，再构造运行调用身份。浏览器或远程调用者传入的 account_id、invoke_from、父应用和目标应用不能覆盖服务端授权结果。

会话管理、重复启动、等待输入、取消和结算由共用执行服务处理。只在某个 Handler 禁止重复运行，会导致其他入口绕过规则。

## commands、stream 与 state

OpenAPIAgentProtocolHandler 把请求交给公共 entry adapter：commands 接受启动与补充答案，stream 订阅指定运行，state 恢复会话公开状态，取消有独立动作。查询和操作都先取得授权会话。

协议使用自己的 JSON 外壳、流式格式和真实 HTTP 状态，不应该再包进普通业务接口的 success_json 数据层。普通管理接口继续使用平台标准响应；客户端应按接口类型解码。

断线重新订阅不是重新调用启动命令。访问 state 获得会话摘要，也不代表已经加载所有历史执行过程，详细恢复见 [事件与前端](03-事件协议与前端流式体验.md)。

## API Key 的真实实现

新 Key 以 `niceagent-v1/` 开头，随机部分来自 `secrets.token_urlsafe(48)`。前缀用于辨识，不是认证强度来源，也不是路由权限。

认证按完整 Key 查询数据库并检查 is_active，再取得关联 Account；不是只校验前缀。因此更换新生成的前缀不自动废除数据库中的旧 Key。

当前 `ApiKeyService` 与模型保存可直接匹配的 Key，不能描述成已经采用只存哈希、一次性展示或完整自动轮换。若实施这些方案，需要同时设计创建响应、管理列表、认证查询和迁移方式。

## 内部协议的限权

Token 载荷包含版本、account_id、parent_app_id、target_app_id 和 exp，以 HMAC-SHA256 签名；解析时校验签名、类型和有效期。

传输构造会移除配置中大小写不同的伪造内部 Token Header，再写入服务端签发值。签名验证成功后，Service 仍查询数据库确认父子应用、发布状态及会话范围。

协议中的 SDK conversation/run 映射到本平台 Conversation/AgentExecution；取消映射到真实 Run 取消。它是满足异步子 Agent 所需操作的适配，不能据此宣称完整实现所有外部 Agent Server API。

## 语音适配

当前 AudioService 的识别路径使用 DashScope Paraformer，将上传音频分块发送并收集识别结果；回答转语音使用 Edge-TTS。语音是输入输出适配，不另外定义一套 Turn/Run 状态。

读取可播报消息时仍需核对会话、应用状态和归属。不同入口的 created_by 可能表示 Account 或 End User，不能统一用 created_by == account.id 取代会话授权。

公众号发布不列为当前已交付能力；存在渠道相关目录或模型不代表产品链路已经实现。

## 技术难点与阅读入口

多入口最难的是身份语义一致，而不是路由数量。入口适配允许协议不同，但资源归属、运行冲突和取消规则应落到共同业务层。

| 文件 | 关注点 |
| --- | --- |
| [认证中间件](../../../internal/middleware/middleware.py) | JWT/API Key 分支 |
| [API Key 服务](../../../internal/service/account/api_key_service.py) | 生成、启停、完整值查询 |
| [WebApp 服务](../../../internal/service/app/web_app_service.py) | 发布入口与应用范围 |
| [开放协议 Handler](../../../internal/handler/agent_protocol/openapi_agent_protocol_handler.py) | 授权身份与公共适配器 |
| [公共入口适配](../../../internal/handler/agent/entry_adapter.py) | command/state/stream/cancel |
| [内部 Token](../../../internal/service/internal_agent_protocol/auth.py) | 签名与限权 |
| [内部协议 Service](../../../internal/service/internal_agent_protocol/service.py) | SDK 操作和实际 Run 映射 |
| [语音服务](../../../internal/service/audio/audio_service.py) | ASR/TTS 与消息授权 |
