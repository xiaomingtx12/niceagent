# 智谱AI（GLM）模型集成完成 ✅

## 集成概述

已成功将智谱AI的免费模型集成到系统中。智谱AI提供完全免费的GLM系列模型，支持工具调用和Agent功能，非常适合开发测试和小规模应用。

## 集成的模型

### 1. GLM-4.7-Flash（推荐）⭐

- **完全免费** ✅
- **128K 上下文窗口**
- **8K 最大输出**
- **支持工具调用**
- **代码能力强**
- **推理速度快**

### 2. GLM-4-Flash

- **完全免费** ✅
- **128K 上下文窗口**
- **4K 最大输出**
- **支持工具调用**
- **快速响应**

## 完成的工作

### 1. 创建提供商目录

```
my-dify/internal/core/language_model/providers/zhipu/
├── __init__.py
├── chat.py
├── positions.yaml
├── glm-4.7-flash.yaml
├── glm-4-flash.yaml
└── _asset/
```

### 2. 配置文件

✅ **chat.py**: 智谱AI聊天模型类
```python
from langchain_community.chat_models import ChatZhipuAI

class Chat(ChatZhipuAI, BaseLanguageModel):
    """智谱AI聊天模型"""
    pass
```

✅ **glm-4.7-flash.yaml**: GLM-4.7-Flash 模型配置
```yaml
model: glm-4.7-flash
label: GLM-4.7-Flash (免费)
features:
  - tool_call
  - agent_thought
context_window: 128000
max_output_tokens: 8192
```

✅ **glm-4-flash.yaml**: GLM-4-Flash 模型配置
```yaml
model: glm-4-flash
label: GLM-4-Flash (免费)
features:
  - tool_call
  - agent_thought
context_window: 128000
max_output_tokens: 4096
```

### 3. 更新提供商列表

✅ 在 `providers.yaml` 中添加智谱AI：

```yaml
- name: zhipu
  label: 智谱AI
  description: 智谱AI提供的免费模型，例如GLM-4-Flash、GLM-4.7-Flash等，支持工具调用。
  icon: icon.png
  background: "#FFFFFF"
  supported_model_types:
    - chat
```

### 4. 文档

✅ **docs/31.智谱AI模型集成说明.md**: 详细集成文档
- 模型特性介绍
- API 配置说明
- 使用示例
- 常见问题

## 使用方法

### 1. 获取 API Key

1. 访问 [智谱AI开放平台](https://open.bigmodel.cn/)
2. 注册/登录账号
3. 创建 API Key

### 2. 配置 API Key

在系统设置中：
```
设置 > API 密钥管理 > 添加 API 密钥
提供商: 智谱AI
API Key: your-api-key
```

### 3. 使用模型

在应用配置中选择：
- 提供商: `zhipu`
- 模型: `glm-4.7-flash` 或 `glm-4-flash`

### 4. 代码调用

```python
from internal.core.language_model.providers.zhipu.chat import Chat
from internal.core.language_model.entities.model_entity import ModelFeature
from internal.lib.api_key_helper import get_api_key_for_provider

# 获取 API Key
api_key = get_api_key_for_provider("zhipu", account.id)

# 创建模型实例
llm = Chat(
    model="glm-4.7-flash",
    features=[ModelFeature.TOOL_CALL, ModelFeature.AGENT_THOUGHT],
    metadata={},
    api_key=api_key,
)

# 调用模型
response = llm.invoke("你好")
```

## 模型对比

| 特性 | GLM-4.7-Flash | GLM-4-Flash | Qwen-Plus | GPT-4o-mini |
|------|---------------|-------------|-----------|-------------|
| 价格 | 免费 ✅ | 免费 ✅ | 付费 | 付费 |
| 上下文 | 128K | 128K | 128K | 128K |
| 最大输出 | 8K | 4K | 8K | 16K |
| 工具调用 | ✅ | ✅ | ✅ | ✅ |
| 代码能力 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ |
| 推荐场景 | 代码生成、Agent | 通用对话 | 复杂任务 | 通用任务 |

## 核心优势

### 1. 完全免费 💰

- 无需付费
- 无限制使用（可能有 QPM 限制）
- 降低开发成本

### 2. 性能强大 🚀

- GLM-4.7-Flash 代码能力优秀
- 128K 上下文窗口
- 支持工具调用和 Agent

### 3. 易于集成 🔧

- OpenAI SDK 兼容
- 简单的 API 调用
- 完善的文档

## 使用场景

### 1. 开发测试

免费模型非常适合：
- 功能开发和测试
- 原型验证
- 学习和实验

### 2. 小规模应用

适合：
- 个人项目
- 小型应用
- 内部工具

### 3. 代码生成

GLM-4.7-Flash 特别适合：
- 代码补全
- 代码解释
- Bug 修复
- 代码重构

### 4. Agent 应用

支持：
- 工具调用
- 多步推理
- 任务规划

## 系统支持的模型列表

集成智谱AI后，系统现在支持 6 个提供商，共 12+ 个模型：

1. **OpenAI**: gpt-4o, gpt-4o-mini
2. **Moonshot**: moonshot-v1-8k, moonshot-v1-32k, moonshot-v1-128k
3. **Tongyi**: qwen3.5-plus, qwen-plus, qwen-max
4. **Zhipu**: glm-4.7-flash ⭐, glm-4-flash ⭐ (免费)
5. **Ollama**: qwen2.5-7b
6. **Wenxin**: ernie-3.5-8k, ernie-3.5-128k, ernie-4.0-8k

## 技术细节

### API Endpoint

```
Base URL: https://open.bigmodel.cn/api/paas/v4/
```

### 认证方式

```python
headers = {
    "Authorization": f"Bearer {api_key}",
    "Content-Type": "application/json"
}
```

### 兼容性

- ✅ OpenAI SDK 兼容
- ✅ LangChain 支持
- ✅ 流式输出
- ✅ 工具调用

## 性能优化

### 1. 模型选择

- **代码任务**: GLM-4.7-Flash
- **通用对话**: GLM-4-Flash
- **复杂推理**: 考虑付费模型

### 2. 参数调优

```python
# 代码生成
llm = Chat(model="glm-4.7-flash", temperature=0.3)

# 创意写作
llm = Chat(model="glm-4.7-flash", temperature=0.9)
```

### 3. 流式输出

```python
llm = Chat(model="glm-4.7-flash", streaming=True)
for chunk in llm.stream("写一篇文章"):
    print(chunk, end="", flush=True)
```

## 常见问题

### Q1: 免费模型有限制吗？

可能有 QPM（每分钟请求数）限制，具体请查看官方文档。

### Q2: 如何获取 API Key？

访问 [智谱AI开放平台](https://open.bigmodel.cn/)，注册后创建。

### Q3: 支持哪些功能？

- ✅ 文本生成
- ✅ 工具调用
- ✅ Agent 思维链
- ✅ 流式输出
- ✅ 多轮对话

### Q4: 与其他模型相比如何？

GLM-4.7-Flash 在代码生成方面表现优异，且完全免费，非常适合开发测试。

## 文件清单

### 新增文件

```
my-dify/
├── internal/core/language_model/providers/zhipu/
│   ├── __init__.py
│   ├── chat.py
│   ├── positions.yaml
│   ├── glm-4.7-flash.yaml
│   ├── glm-4-flash.yaml
│   └── _asset/
├── docs/
│   └── 31.智谱AI模型集成说明.md
└── ZHIPU_AI_INTEGRATION.md (本文件)
```

### 修改文件

```
my-dify/
└── internal/core/language_model/providers/
    └── providers.yaml (添加智谱AI)
```

## 后续工作

### 可选增强

- [ ] 添加更多智谱AI模型（付费模型）
- [ ] 添加视觉模型（GLM-4.6V-Flash）
- [ ] 优化错误处理
- [ ] 添加使用统计

### 用户体验

- [ ] 在前端标注免费模型
- [ ] 添加模型推荐功能
- [ ] 提供使用指南

## 参考资料

- [智谱AI开放平台](https://open.bigmodel.cn/)
- [GLM-4.6 文档](https://docs.bigmodel.cn/cn/guide/models/text/glm-4.6)
- [API 文档](https://open.bigmodel.cn/dev/api)
- [定价信息](https://open.bigmodel.cn/pricing)
- [LangChain 集成](https://python.langchain.com/docs/integrations/chat/zhipuai)

## 更新日志

### 2026-02-21

- ✅ 完成智谱AI模型集成
- ✅ 添加 GLM-4.7-Flash 模型
- ✅ 添加 GLM-4-Flash 模型
- ✅ 更新提供商列表
- ✅ 创建集成文档

---

**集成状态**: ✅ 完成  
**免费模型**: GLM-4.7-Flash, GLM-4-Flash  
**推荐使用**: GLM-4.7-Flash（性能更强，完全免费）  
**开始使用**: 获取 API Key → 配置 → 选择模型 → 开始构建！ 🚀
