# My Dify

一个基于 Flask、LangChain、LangGraph 构建的 AI 应用开发平台，支持应用管理、工作流编排、知识库检索、MCP 工具接入、Web/API 对话、辅助
Agent、语音转换等能力。

## 核心能力

- 应用管理：创建、编辑、发布、复制、回退应用配置
- 对话能力：支持调试对话、Web App 对话、OpenAPI 对话
- 工作流编排：支持工作流草稿、调试、发布与模板创建
- 知识库：支持数据集、文档、分段管理与检索
- 工具扩展：支持内置工具、自定义 API 工具、MCP Server 工具
- 模型接入：支持多供应商语言模型配置
- 辅助能力：提示词优化、推荐问题、语音转文字、文字转语音
- 账号与鉴权：登录/登出、OAuth 授权、API Key 管理
- 文件能力：文件/图片上传、文档解析与分段
- 可观测性：支持会话、消息、推理轨迹与应用分析

## 技术栈

- Flask
- Flask-Login
- Flask-Migrate
- Flask-SQLAlchemy
- Redis
- Celery
- Weaviate
- FAISS
- LangChain / LangGraph
- MCP

## 项目结构

- `app/`：HTTP 入口与应用启动
- `internal/`：核心业务代码、服务、路由、模型、处理器
- `config/`：应用配置
- `scripts/`：辅助脚本
- `test/`：测试代码

## 快速开始

### 1. 安装依赖

建议使用虚拟环境后安装依赖：

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

#### 依赖服务

运行前建议先准备：

- 数据库：通过 `SQLALCHEMY_DATABASE_URI` 配置（项目依赖包含 `psycopg2-binary`，常见搭配为 PostgreSQL）
- Redis：用于缓存与 Celery
- Weaviate：用于向量检索

项目通过环境变量读取配置，常用项如下：

- `SQLALCHEMY_DATABASE_URI`
- `REDIS_HOST`
- `REDIS_PORT`
- `REDIS_USERNAME`
- `REDIS_PASSWORD`
- `WEAVIATE_HOST`
- `WEAVIATE_PORT`
- `TENCENT_SECRET_ID`
- `TENCENT_SECRET_KEY`
- `TENCENT_SCF_REGION`

可在项目根目录创建 `.env` 文件，例如：

```env
SQLALCHEMY_DATABASE_URI=postgresql+psycopg2://user:password@127.0.0.1:5432/my_dify
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
WEAVIATE_HOST=127.0.0.1
WEAVIATE_PORT=8080
```

### 3. 启动开发服务

```bash
python run.py
```

默认会启动在 `0.0.0.0:5000`。

### 4. 生产环境启动

项目提供 `wsgi.py`，可配合 gunicorn / uWSGI 使用。

示例：

```bash
gunicorn -w 4 -b 0.0.0.0:5000 wsgi:application
```

### 5. Docker 启动（可选）

项目包含 Dockerfile，可用于快速启动 API 或 Celery Worker（由容器环境变量 `MODE` 控制）。

```bash
docker build -t my-dify .
docker run --rm -p 5001:5001 my-dify
```

容器内默认端口为 `5001`（可通过 `LLMOPS_PORT` 调整）。

## 常用接口概览

- `GET /ping`：健康检查
- `GET /apps`：应用列表
- `POST /apps`：创建应用
- `POST /apps/<id>/publish`：发布应用
- `POST /apps/<id>/conversations`：调试对话
- `POST /openapi/chat`：开放 API 对话
- `GET /datasets`：知识库列表
- `GET /workflows`：工作流列表
- `POST /assistant-agent/chat`：辅助 Agent 对话

更多接口可参考 `internal/router/router.py`。

## 配置说明

主要配置定义在 `config/config.py` 与 `config/default_config.py`，包括：

- 数据库连接
- Redis / Celery
- Weaviate 向量库
- 辅助 Agent 应用 ID
- 腾讯云代码执行配置

## 说明

本项目提供一套可扩展的 AI 应用后端服务能力，可按需组合模型、工具、工作流与知识库，面向 Web 与 OpenAPI 场景提供对话与管理接口。
