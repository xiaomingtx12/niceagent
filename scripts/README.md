# Faiss 知识库导入工具

## 使用方法

### 1. 获取 DashScope API Key

访问 [DashScope 控制台](https://dashscope.console.aliyun.com/) 获取 API Key

### 2. 修改配置

打开 `scripts/import_to_faiss.py`，修改第24行：

```python
DASHSCOPE_API_KEY = "your-dashscope-api-key"  # 👈 在这里填写你的 API Key
```

可选：修改源目录（默认为 `storage/vector_store`）

```python
SOURCE_DIR = "storage/vector_store"  # 或 "docs"
```

### 3. 运行导入

```bash
cd my-dify
python scripts/import_to_faiss.py
```

## 支持的文件格式

- `.txt` - 纯文本文件
- `.md` - Markdown 文件

## 注意事项

1. 脚本独立运行，不依赖 Flask 应用
2. 直接使用 DashScope API Key，无需配置数据库用户
3. 导入前建议备份：`cp -r internal/core/vector_store internal/core/vector_store.backup`

## 示例

```bash
# 1. 准备文档
mkdir -p storage/vector_store
echo "这是测试文档" > storage/vector_store/test.txt

# 2. 修改 scripts/import_to_faiss.py 中的 DASHSCOPE_API_KEY

# 3. 运行导入
python scripts/import_to_faiss.py
```

## 验证导入

启动应用后，与辅助Agent对话，询问知识库相关的问题，验证是否能检索到导入的内容。
