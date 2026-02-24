# 用户 API Key 管理功能实现总结

## 功能说明

实现了用户自主配置 AI 模型提供商 API Key 的功能。系统强制要求用户配置自己的 API Key，不再使用环境变量中的默认密钥，确保每个用户使用自己的资源。

## 新增文件

### 1. 数据模型

- `my-dify/internal/model/provider_api_key.py` - 提供商 API Key 数据模型

### 2. 服务层

- `my-dify/internal/service/provider_api_key_service.py` - 提供商 API Key 业务逻辑服务

### 3. Schema 层

- `my-dify/internal/schema/provider_api_key_schema.py` - 请求/响应数据结构定义

### 4. Handler 层

- `my-dify/internal/handler/provider_api_key_handler.py` - API 请求处理器

### 5. 工具函数

- `my-dify/internal/lib/api_key_helper.py` - API Key 获取辅助函数

### 6. 数据库迁移

- `my-dify/migrations/add_provider_api_key_table.sql` - 数据库表创建脚本

### 7. 文档

- `my-dify/docs/27.用户API密钥管理功能.md` - 功能详细文档
- `my-dify/docs/28.用户API密钥使用示例.md` - 使用示例和前端集成
- `my-dify/USER_API_KEY_FEATURE.md` - 本文件

## 修改的文件

### 1. 模型导入

- `my-dify/internal/model/__init__.py` - 添加 ProviderApiKey 模型导入

### 2. 服务导入

- `my-dify/internal/service/__init__.py` - 添加 ProviderApiKeyService 服务导入

### 3. Handler 导入

- `my-dify/internal/handler/__init__.py` - 添加 ProviderApiKeyHandler 处理器导入

### 4. 路由配置

- `my-dify/internal/router/router.py` - 添加提供商 API Key 相关路由

### 5. 应用服务

- `my-dify/internal/service/app_service.py` - 修改为使用用户配置的 API Key

## API 接口

| 方法   | 路径                             | 说明                |
|------|--------------------------------|-------------------|
| GET  | /provider-api-keys             | 获取 API Key 列表（分页） |
| POST | /provider-api-keys             | 创建 API Key        |
| GET  | /provider-api-keys/{id}        | 获取单个 API Key 详情   |
| POST | /provider-api-keys/{id}        | 更新 API Key        |
| POST | /provider-api-keys/{id}/delete | 删除 API Key        |

## 数据库变更

### 新增表：provider_api_key

```sql
CREATE TABLE provider_api_key (
    id UUID PRIMARY KEY,
    account_id UUID NOT NULL,
    provider_name VARCHAR(255) NOT NULL,
    api_key VARCHAR(500) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT true,
    remark VARCHAR(255) NOT NULL DEFAULT '',
    updated_at TIMESTAMP(0) NOT NULL,
    created_at TIMESTAMP(0) NOT NULL
);
```

### 索引

- `provider_api_key_account_id_idx` - 账号 ID 索引
- `provider_api_key_account_provider_idx` - 账号 ID + 提供商名称联合索引

## 部署步骤

### 1. 执行数据库迁移

```bash
# 连接到数据库
psql -U myuser -h 106.54.40.123 -d my_dify

# 执行迁移脚本
\i migrations/add_provider_api_key_table.sql

# 或者直接执行
psql -U myuser -h 106.54.40.123 -d my_dify -f migrations/add_provider_api_key_table.sql
```

### 2. 重启应用

```bash
# 如果使用 Docker
docker-compose restart

# 如果直接运行
# 停止当前进程，然后重新启动
python app/http/app.py
```

### 3. 验证功能

```bash
# 测试创建 API Key
curl -X POST http://localhost:5000/provider-api-keys \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -d '{
    "provider_name": "tongyi",
    "api_key": "sk-test",
    "is_active": true,
    "remark": "测试密钥"
  }'

# 测试获取列表
curl http://localhost:5000/provider-api-keys?page=1&page_size=20 \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## 使用方式

### 后端代码中使用

```python
from internal.lib.api_key_helper import get_api_key_for_provider

# 在服务中使用
api_key = get_api_key_for_provider(
    provider_name="tongyi",
    account_id=account.id,
    provider_api_key_service=self.provider_api_key_service
)

# 创建模型实例
llm = Chat(
    model="qwen-max",
    api_key=api_key,
    # ... 其他参数
)
```

### 便捷方法（用于类方法）

```python
from internal.lib.api_key_helper import get_dashscope_api_key

# 在类方法中使用
api_key = get_dashscope_api_key(account_id)
```

## 支持的提供商

- dashscope (通义千问)
- openai (OpenAI)
- moonshot (月之暗面)
- zhipu (智谱AI)
- deepseek (DeepSeek)
- qianfan (百度千帆)
- groq (Groq)

## 工作原理

1. 用户通过 API 配置自己的提供商 API Key
2. 系统在调用 AI 模型时，通过 `get_api_key_for_provider()` 函数获取 API Key
3. 函数**仅从用户配置中查找**激活状态的 API Key
4. 如果用户未配置，系统会抛出异常并提示用户先配置 API Key
5. API Key 在响应中自动脱敏显示

**重要变更**：系统不再使用环境变量中的默认 API Key，所有用户必须配置自己的密钥才能使用平台功能。

## 安全性考虑

1. **API Key 存储**：当前明文存储在数据库中，建议后续实现加密存储
2. **访问控制**：用户只能管理自己的 API Key
3. **脱敏显示**：API Key 在列表和详情接口中自动脱敏
4. **认证要求**：所有接口都需要登录认证

## 后续优化建议

1. **加密存储**：使用 AES 等加密算法对 API Key 进行加密存储
2. **有效性验证**：在保存时验证 API Key 是否有效
3. **使用统计**：记录每个用户的 API 调用次数和 Token 消耗
4. **配额管理**：为用户设置使用配额限制
5. **多密钥轮询**：支持配置多个 API Key 并自动轮询使用
6. **审计日志**：记录 API Key 的创建、修改、删除操作
7. **密钥过期**：支持设置 API Key 的有效期
8. **成本分析**：统计每个用户的 API 调用成本

## 测试清单

- [ ] 创建 API Key
- [ ] 获取 API Key 列表
- [ ] 获取单个 API Key 详情
- [ ] 更新 API Key
- [ ] 删除 API Key
- [ ] 使用用户配置的 API Key 调用模型
- [ ] 用户未配置时返回友好的错误提示
- [ ] API Key 脱敏显示
- [ ] 权限控制（用户只能管理自己的 API Key）
- [ ] 每个提供商只能配置一个 API Key 的限制
- [ ] 禁用的 API Key 不会被使用

## 相关文件位置

```
my-dify/
├── internal/
│   ├── model/
│   │   └── provider_api_key.py          # 数据模型
│   ├── service/
│   │   └── provider_api_key_service.py  # 业务逻辑
│   ├── schema/
│   │   └── provider_api_key_schema.py   # 数据结构
│   ├── handler/
│   │   └── provider_api_key_handler.py  # 请求处理
│   ├── lib/
│   │   └── api_key_helper.py            # 辅助函数
│   └── router/
│       └── router.py                     # 路由配置
├── migrations/
│   └── add_provider_api_key_table.sql   # 数据库迁移
└── docs/
    ├── 27.用户API密钥管理功能.md
    └── 28.用户API密钥使用示例.md
```

## 联系方式

如有问题或建议，请联系开发团队。
