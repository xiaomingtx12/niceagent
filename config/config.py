import os
from typing import Any

from config.default_config import DEFAULT_CONFIG


def _get_env(key: str) -> Any:
    """从环境变量中获取配置项，如果找不到则返回默认值"""
    return os.getenv(key, DEFAULT_CONFIG.get(key))


def _get_bool_env(key: str) -> bool:
    """从环境变量中获取布尔值型的配置项，如果找不到则返回默认值"""
    value: str = _get_env(key)
    return value.lower() == "true" if value is not None else False


def _get_workspace_limit(key: str) -> int:
    """读取正数 Workspace 限制，生产环境要求显式配置。"""
    raw = os.getenv(key)
    if os.getenv("FLASK_ENV", "").lower() == "production" and raw is None:
        raise ValueError(f"{key} is required in production")
    value = int(raw if raw is not None else DEFAULT_CONFIG[key])
    if value <= 0:
        raise ValueError("Workspace limits must be greater than zero")
    return value


class Config:
    def __init__(self):
        """从环境变量加载应用配置。"""
        # 关闭wtf的csrf保护
        self.WTF_CSRF_ENABLED = _get_bool_env("WTF_CSRF_ENABLED")

        # 配置数据库配置
        self.SQLALCHEMY_DATABASE_URI = _get_env("SQLALCHEMY_DATABASE_URI")
        self.SQLALCHEMY_ENGINE_OPTIONS = {
            "pool_size": int(_get_env("SQLALCHEMY_POOL_SIZE")),
            "pool_recycle": int(_get_env("SQLALCHEMY_POOL_RECYCLE")),
            "pool_pre_ping": True,
        }
        self.SQLALCHEMY_ECHO = _get_bool_env("SQLALCHEMY_ECHO")
        self.AGENT_CHECKPOINT_BACKEND = str(
            _get_env("AGENT_CHECKPOINT_BACKEND")
        ).lower()
        self.AGENT_MEMORY_BACKEND = str(
            _get_env("AGENT_MEMORY_BACKEND")
        ).lower()
        self.AGENT_SANDBOX_PROVIDER = str(
            _get_env("AGENT_SANDBOX_PROVIDER")
        ).lower()
        self.AGENT_SANDBOX_SNAPSHOT_ID = _get_env(
            "AGENT_SANDBOX_SNAPSHOT_ID"
        ) or None
        self.AGENT_SANDBOX_IDLE_TTL_SECONDS = int(
            _get_env("AGENT_SANDBOX_IDLE_TTL_SECONDS")
        )
        self.AGENT_SANDBOX_DELETE_AFTER_STOP_SECONDS = int(
            _get_env("AGENT_SANDBOX_DELETE_AFTER_STOP_SECONDS")
        )
        self.BUILTIN_SKILLS_ROOT = _get_env("BUILTIN_SKILLS_ROOT")
        self.WORKSPACE_MAX_BYTES = _get_workspace_limit("WORKSPACE_MAX_BYTES")
        self.WORKSPACE_MAX_FILES = _get_workspace_limit("WORKSPACE_MAX_FILES")
        self._validate_agent_settings()

        # Redis配置
        self.REDIS_HOST = _get_env("REDIS_HOST")
        self.REDIS_PORT = _get_env("REDIS_PORT")
        self.REDIS_USERNAME = _get_env("REDIS_USERNAME")
        self.REDIS_PASSWORD = _get_env("REDIS_PASSWORD")
        self.REDIS_DB = _get_env("REDIS_DB")
        self.REDIS_USE_SSL = _get_bool_env("REDIS_USE_SSL")
        self.REDIS_MAX_CONNECTIONS = int(_get_env("REDIS_MAX_CONNECTIONS"))
        self.REDIS_SOCKET_TIMEOUT = float(_get_env("REDIS_SOCKET_TIMEOUT"))
        self.REDIS_SOCKET_CONNECT_TIMEOUT = float(
            _get_env("REDIS_SOCKET_CONNECT_TIMEOUT")
        )
        self.REDIS_HEALTH_CHECK_INTERVAL = int(
            _get_env("REDIS_HEALTH_CHECK_INTERVAL")
        )

        # Celery配置
        self.CELERY = {
            "broker_url": f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{int(_get_env('CELERY_BROKER_DB'))}",
            "result_backend": f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{int(_get_env('CELERY_RESULT_BACKEND_DB'))}",
            "task_ignore_result": _get_bool_env("CELERY_TASK_IGNORE_RESULT"),
            "result_expires": int(_get_env("CELERY_RESULT_EXPIRES")),
            "broker_connection_retry_on_startup": _get_bool_env(
                "CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP"
            ),
            # 连接重试配置
            "broker_connection_retry": True,
            "broker_connection_max_retries": 10,
            # Redis 连接池配置
            "broker_transport_options": {
                "socket_timeout": 30,
                "socket_connect_timeout": 10,
                "socket_keepalive": True,
                "retry_on_timeout": True,
                "max_connections": 50,
                "health_check_interval": 30,
            },
            "redis_socket_timeout": 30,
            "redis_socket_connect_timeout": 10,
        }

        # 辅助Agent应用id标识
        self.ASSISTANT_AGENT_ID = _get_env("ASSISTANT_AGENT_ID")

        # Weaviate向量数据库配置
        self.WEAVIATE_HTTP_HOST = _get_env("WEAVIATE_HOST")
        self.WEAVIATE_HTTP_PORT = int(_get_env("WEAVIATE_PORT"))
        self.WEAVIATE_HTTP_SECURE = _get_bool_env("WEAVIATE_HTTP_SECURE")
        self.WEAVIATE_GRPC_HOST = _get_env("WEAVIATE_HOST")
        self.WEAVIATE_GRPC_PORT = int(_get_env("WEAVIATE_GRPC_PORT"))
        # 跳过初始化检查以减少不必要的请求
        self.WEAVIATE_SKIP_INIT_CHECKS = _get_bool_env("WEAVIATE_SKIP_INIT_CHECKS")

        # 腾讯云配置（用于云函数代码执行）
        self.TENCENT_SECRET_ID = _get_env("TENCENT_SECRET_ID")
        self.TENCENT_SECRET_KEY = _get_env("TENCENT_SECRET_KEY")
        self.TENCENT_SCF_REGION = _get_env("TENCENT_SCF_REGION")
        self.TENCENT_SCF_NAMESPACE = _get_env("TENCENT_SCF_NAMESPACE")
        self.TENCENT_SCF_FUNCTION_NAME = _get_env("TENCENT_SCF_FUNCTION_NAME")

    def _validate_agent_settings(self) -> None:
        """校验 Agent 持久化和记忆配置。"""
        supported = {"postgres", "memory", "disabled", "false", "off", "none"}
        if self.AGENT_CHECKPOINT_BACKEND not in supported:
            raise ValueError(
                "AGENT_CHECKPOINT_BACKEND 必须是 postgres、memory 或 disabled"
            )
        if self.AGENT_MEMORY_BACKEND not in {"postgres", "memory"}:
            raise ValueError(
                "AGENT_MEMORY_BACKEND 不允许关闭，必须是 postgres 或 memory"
            )
        if (
            os.getenv("FLASK_ENV", "").lower() == "production"
            and self.AGENT_CHECKPOINT_BACKEND != "postgres"
        ):
            raise ValueError("生产环境必须使用 postgres checkpoint")
        if self.AGENT_SANDBOX_PROVIDER not in {"disabled", "langsmith"}:
            raise ValueError(
                "AGENT_SANDBOX_PROVIDER 必须是 disabled 或 langsmith"
            )
        if (
            os.getenv("FLASK_ENV", "").lower() == "production"
            and self.AGENT_MEMORY_BACKEND != "postgres"
        ):
            raise ValueError("生产环境必须使用 postgres memory store")
        for name in (
            "AGENT_SANDBOX_IDLE_TTL_SECONDS",
            "AGENT_SANDBOX_DELETE_AFTER_STOP_SECONDS",
        ):
            value = getattr(self, name)
            if value < 0 or value % 60 != 0:
                raise ValueError(f"{name} 必须是 60 的倍数或 0")
