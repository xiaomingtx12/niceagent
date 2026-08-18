# 使用Debian 12的Python 3.11版本作为基础镜像
FROM python:3.11-slim-bookworm AS base

# 配置apt使用阿里云镜像源加速
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources && \
    apt-get update

# 将requirements.txt拷贝到根目录下
COPY requirements.txt .

# 升级pip并安装依赖（使用清华镜像源加速）
RUN --mount=type=cache,target=/root/.cache/pip \
    pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple && \
    pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn && \
    pip install --upgrade pip && \
    pip install --prefix=/pkg -r requirements.txt

# 二阶段生产环境构建
FROM python:3.11-slim-bookworm AS production

# DeepAgents execute tool uses Bubblewrap for mount/PID/user/network isolation.
RUN apt-get update && \
    apt-get install -y --no-install-recommends bubblewrap && \
    rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app/api

# 定义环境变量
ENV FLASK_APP=app/http/app.py
ENV FLASK_ENV=production
ENV FLASK_DEBUG=0
ENV NLTK_DATA=/app/api/internal/core/unstructured/nltk_data
ENV HF_ENDPOINT=https://hf-mirror.com
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ENV PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn
ENV BUILTIN_SKILLS_ROOT=/app/api/resources/agent_skills/builtin

# 设置容器时区为中国标准时间，避免时区错误
ENV TZ=Asia/Shanghai

# 拷贝第三方依赖包+源码文件
COPY --from=base /pkg /usr/local
COPY . /app/api

# Builtin Skills are versioned image content and never a runtime write area.
RUN useradd --create-home --uid 10001 app && \
    chown -R app:app /app/api && \
    chown -R root:root /app/api/resources/agent_skills/builtin && \
    chmod -R a-w /app/api/resources/agent_skills/builtin

# 拷贝运行脚本并设置权限
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER app

# 暴露5001端口
EXPOSE 5001

# 运行脚本并启动项目
ENTRYPOINT ["/bin/bash", "/entrypoint.sh"]
