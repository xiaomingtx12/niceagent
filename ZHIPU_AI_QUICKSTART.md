# 智谱AI 快速开始指南

## 🚀 3分钟快速上手

### 步骤 1: 获取 API Key（1分钟）

1. 访问 [智谱AI开放平台](https://open.bigmodel.cn/)
2. 注册/登录账号
3. 进入"API Keys"页面
4. 点击"创建新的 API Key"
5. 复制保存 API Key

### 步骤 2: 配置 API Key（30秒）

在系统中配置：

```
设置 > API 密钥管理 > 添加 API 密钥
提供商: 智谱AI
API Key: 粘贴你的 API Key
保存
```

### 步骤 3: 使用模型（30秒）

在应用配置中选择：

- 提供商: `智谱AI`
- 模型: `GLM-4.7-Flash (免费)` ⭐ 推荐

完成！开始使用完全免费的AI模型！

## 💡 为什么选择智谱AI？

### 1. 完全免费 ✅

- GLM-4.7-Flash: 免费
- GLM-4-Flash: 免费
- 无需信用卡
- 无隐藏费用

### 2. 性能强大 🚀

- 128K 上下文窗口
- 支持工具调用
- 代码能力优秀
- 推理速度快

### 3. 易于使用 🎯

- OpenAI SDK 兼容
- 简单的 API 调用
- 完善的文档

## 📊 模型选择

### GLM-4.7-Flash（推荐）⭐

**适合场景**:
- ✅ 代码生成和解释
- ✅ 复杂推理任务
- ✅ Agent 应用
- ✅ 工具调用

**特点**:
- 128K 上下文
- 8K 最大输出
- 代码能力强
- 完全免费

### GLM-4-Flash

**适合场景**:
- ✅ 通用对话
- ✅ 文本生成
- ✅ 快速响应
- ✅ 简单任务

**特点**:
- 128K 上下文
- 4K 最大输出
- 响应快速
- 完全免费

## 🎯 使用示例

### 1. 智能对话

```python
from internal.core.language_model.providers.zhipu.chat import Chat

llm = Chat(
    model="glm-4.7-flash",
    api_key="your-api-key",
)

response = llm.invoke("介绍一下人工智能")
print(response)
```

### 2. 代码生成

```python
llm = Chat(
    model="glm-4.7-flash",
    temperature=0.3,  # 降低随机性
    api_key="your-api-key",
)

response = llm.invoke("写一个 Python 快速排序函数")
print(response)
```

### 3. 工具调用

```python
from langchain.tools import Tool

tools = [
    Tool(
        name="calculator",
        func=lambda x: eval(x),
        description="用于数学计算"
    )
]

llm = Chat(
    model="glm-4.7-flash",
    api_key="your-api-key",
)

response = llm.invoke("计算 123 * 456", tools=tools)
```

## 🔧 常见问题

### Q: 免费模型有使用限制吗？

A: 可能有 QPM（每分钟请求数）限制，但对于大多数应用足够使用。

### Q: 如何获取更多免费额度？

A: GLM-4.7-Flash 和 GLM-4-Flash 是完全免费的，无需额外申请。

### Q: 支持哪些功能？

A: 
- ✅ 文本生成
- ✅ 工具调用（Function Calling）
- ✅ Agent 思维链
- ✅ 流式输出
- ✅ 多轮对话

### Q: 与其他免费模型相比如何？

A: GLM-4.7-Flash 在代码生成方面表现优异，且完全免费，非常适合开发测试。

### Q: 如何切换模型？

A: 在应用配置的"模型设置"中选择不同的模型即可。

## 📚 进阶使用

### 流式输出

```python
llm = Chat(
    model="glm-4.7-flash",
    streaming=True,
    api_key="your-api-key",
)

for chunk in llm.stream("写一篇关于AI的文章"):
    print(chunk, end="", flush=True)
```

### 参数调优

```python
# 代码生成（降低随机性）
llm = Chat(model="glm-4.7-flash", temperature=0.3)

# 创意写作（增加随机性）
llm = Chat(model="glm-4.7-flash", temperature=0.9)

# 平衡模式
llm = Chat(model="glm-4.7-flash", temperature=0.7)
```

### 多轮对话

```python
messages = [
    {"role": "system", "content": "你是一个编程助手"},
    {"role": "user", "content": "什么是快速排序？"},
    {"role": "assistant", "content": "快速排序是..."},
    {"role": "user", "content": "请用Python实现"},
]

response = llm.invoke(messages)
```

## 🎓 学习资源

### 官方文档

- [智谱AI开放平台](https://open.bigmodel.cn/)
- [GLM-4.6 文档](https://docs.bigmodel.cn/cn/guide/models/text/glm-4.6)
- [API 文档](https://open.bigmodel.cn/dev/api)

### 系统文档

- [详细集成说明](./docs/31.智谱AI模型集成说明.md)
- [集成总结](./ZHIPU_AI_INTEGRATION.md)

## 💰 成本对比

| 模型 | 价格 | 上下文 | 适用场景 |
|------|------|--------|---------|
| GLM-4.7-Flash | 免费 ✅ | 128K | 代码、Agent |
| GLM-4-Flash | 免费 ✅ | 128K | 通用对话 |
| Qwen-Plus | 付费 | 128K | 复杂任务 |
| GPT-4o-mini | 付费 | 128K | 通用任务 |

## 🚀 开始使用

1. **获取 API Key**: [智谱AI开放平台](https://open.bigmodel.cn/)
2. **配置系统**: 设置 > API 密钥管理
3. **选择模型**: GLM-4.7-Flash (推荐)
4. **开始构建**: 完全免费的AI应用！

---

**开始使用智谱AI，体验完全免费的强大AI模型！** 🎉

**推荐模型**: GLM-4.7-Flash  
**价格**: 完全免费  
**特点**: 代码能力强、支持工具调用、128K上下文
