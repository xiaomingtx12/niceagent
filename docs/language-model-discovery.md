# 第三方模型动态发现

当前第三方模型集成保留静态 YAML 模型，同时允许用户在填写供应商 API Key
时显式发现该供应商返回的模型，并选择需要激活的模型。

## 配置

在 `internal/core/language_model/providers/providers.yaml` 的供应商节点增加：

```yaml
base_url: https://provider.example/v1
model_discovery:
  enabled: true
  endpoint: /models
  fallback_endpoints:
    - /v1/models
  timeout_seconds: 5
  cache_ttl_seconds: 300
```

用户点击“发现模型”时，后端使用本次尚未保存的 API Key 请求目录。默认使用
`Authorization: Bearer <api_key>`，也可以通过 `api_key_header` 和
`api_key_prefix` 适配其他 OpenAI-compatible 网关。

## 运行行为

- `POST /language-models/discover` 是唯一会主动请求第三方模型目录的接口。
- `GET /language-models` 只读取静态模型和当前账号已保存且已激活的动态模型。
- API Key 创建请求保存 `discovered_models` 快照和 `enabled_models` 激活列表。
- 更新 API Key 会清空旧模型快照，避免新密钥沿用旧密钥的模型权限。
- `POST /provider-api-keys/<id>/models` 只更新当前快照中的激活模型。
- 静态模型优先保留人工维护的能力、上下文和价格信息。
- 动态发现结果按供应商名和 API Key 摘要使用短期进程缓存，但不会在普通模型列表请求中触发发现。
- 动态模型调用使用 LangChain 通用 OpenAI-compatible 模型类，并使用供应商配置的 `base_url`。

动态目录返回的未知字段不会直接暴露给前端，只提取模型标识、能力、上下文限制、
输出限制、归属信息和价格等平台字段。
