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

# 设置容器时区为中国标准时间，避免时区错误
ENV TZ=Asia/Shanghai

# 拷贝第三方依赖包+源码文件
COPY --from=base /pkg /usr/local
COPY . /app/api

# 拷贝运行脚本并设置权限
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 暴露5001端口
EXPOSE 5001

# 运行脚本并启动项目
ENTRYPOINT ["/bin/bash", "/entrypoint.sh"]
