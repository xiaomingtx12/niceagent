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
        self.SQLALCHEMY_DATABASE_URI = str(_get_env("SQLALCHEMY_DATABASE_URI") or "")
        self.SQLALCHEMY_POOL_SIZE = int(_get_env("SQLALCHEMY_POOL_SIZE"))
        self.SQLALCHEMY_MAX_OVERFLOW = int(_get_env("SQLALCHEMY_MAX_OVERFLOW"))
        self.SQLALCHEMY_POOL_TIMEOUT_SECONDS = float(
            _get_env("SQLALCHEMY_POOL_TIMEOUT_SECONDS")
        )
        self.SQLALCHEMY_POOL_RECYCLE = int(_get_env("SQLALCHEMY_POOL_RECYCLE"))
        self.POSTGRES_CONNECT_TIMEOUT_SECONDS = int(
            _get_env("POSTGRES_CONNECT_TIMEOUT_SECONDS")
        )
        self.POSTGRES_KEEPALIVES_IDLE_SECONDS = int(
            _get_env("POSTGRES_KEEPALIVES_IDLE_SECONDS")
        )
        self.POSTGRES_KEEPALIVES_INTERVAL_SECONDS = int(
            _get_env("POSTGRES_KEEPALIVES_INTERVAL_SECONDS")
        )
        self.POSTGRES_KEEPALIVES_COUNT = int(_get_env("POSTGRES_KEEPALIVES_COUNT"))
        self.POSTGRES_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS = int(
            _get_env("POSTGRES_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS")
        )
        self.POSTGRES_POOL_MAX_LIFETIME_SECONDS = float(
            _get_env("POSTGRES_POOL_MAX_LIFETIME_SECONDS")
        )
        self.POSTGRES_POOL_MAX_IDLE_SECONDS = float(
            _get_env("POSTGRES_POOL_MAX_IDLE_SECONDS")
        )
        self.POSTGRES_POOL_RECONNECT_TIMEOUT_SECONDS = float(
            _get_env("POSTGRES_POOL_RECONNECT_TIMEOUT_SECONDS")
        )
        self.SQLALCHEMY_ENGINE_OPTIONS = {
            "pool_size": self.SQLALCHEMY_POOL_SIZE,
            "max_overflow": self.SQLALCHEMY_MAX_OVERFLOW,
            "pool_timeout": self.SQLALCHEMY_POOL_TIMEOUT_SECONDS,
            "pool_recycle": self.SQLALCHEMY_POOL_RECYCLE,
            "pool_pre_ping": True,
            "pool_use_lifo": True,
        }
        if self.SQLALCHEMY_DATABASE_URI.startswith("postgresql"):
            self.SQLALCHEMY_ENGINE_OPTIONS["connect_args"] = {
                "application_name": "my-dify-orm",
                "connect_timeout": self.POSTGRES_CONNECT_TIMEOUT_SECONDS,
                "keepalives": 1,
                "keepalives_idle": self.POSTGRES_KEEPALIVES_IDLE_SECONDS,
                "keepalives_interval": self.POSTGRES_KEEPALIVES_INTERVAL_SECONDS,
                "keepalives_count": self.POSTGRES_KEEPALIVES_COUNT,
                "options": (
                    "-c idle_in_transaction_session_timeout="
                    f"{self.POSTGRES_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS * 1000}"
                ),
            }
        self.SQLALCHEMY_ECHO = _get_bool_env("SQLALCHEMY_ECHO")
        self.AGENT_RUN_MAX_WORKERS = int(_get_env("AGENT_RUN_MAX_WORKERS"))
        self.AGENT_CHECKPOINT_BACKEND = str(
            _get_env("AGENT_CHECKPOINT_BACKEND")
        ).lower()
        self.AGENT_CHECKPOINT_POOL_MIN_SIZE = int(
            _get_env("AGENT_CHECKPOINT_POOL_MIN_SIZE")
        )
        self.AGENT_CHECKPOINT_POOL_MAX_SIZE = int(
            _get_env("AGENT_CHECKPOINT_POOL_MAX_SIZE")
        )
        self.AGENT_CHECKPOINT_POOL_TIMEOUT_SECONDS = float(
            _get_env("AGENT_CHECKPOINT_POOL_TIMEOUT_SECONDS")
        )
        self.AGENT_CHECKPOINT_POOL_MAX_WAITING = int(
            _get_env("AGENT_CHECKPOINT_POOL_MAX_WAITING")
        )
        self.AGENT_MEMORY_BACKEND = str(_get_env("AGENT_MEMORY_BACKEND")).lower()
        self.AGENT_MEMORY_POOL_MIN_SIZE = int(_get_env("AGENT_MEMORY_POOL_MIN_SIZE"))
        self.AGENT_MEMORY_POOL_MAX_SIZE = int(_get_env("AGENT_MEMORY_POOL_MAX_SIZE"))
        self.AGENT_MEMORY_POOL_TIMEOUT_SECONDS = float(
            _get_env("AGENT_MEMORY_POOL_TIMEOUT_SECONDS")
        )
        self.AGENT_MEMORY_POOL_MAX_WAITING = int(
            _get_env("AGENT_MEMORY_POOL_MAX_WAITING")
        )
        self.AGENT_SANDBOX_PROVIDER = str(_get_env("AGENT_SANDBOX_PROVIDER")).lower()
        self.AGENT_SANDBOX_SNAPSHOT_ID = _get_env("AGENT_SANDBOX_SNAPSHOT_ID") or None
        self.AGENT_SANDBOX_IDLE_TTL_SECONDS = int(
            _get_env("AGENT_SANDBOX_IDLE_TTL_SECONDS")
        )
        self.AGENT_SANDBOX_DELETE_AFTER_STOP_SECONDS = int(
            _get_env("AGENT_SANDBOX_DELETE_AFTER_STOP_SECONDS")
        )
        self.AGENT_SANDBOX_ALLOW_NETWORK = _get_bool_env("AGENT_SANDBOX_ALLOW_NETWORK")
        self.AGENT_SANDBOX_EXECUTE_TIMEOUT_SECONDS = int(
            _get_env("AGENT_SANDBOX_EXECUTE_TIMEOUT_SECONDS")
        )
        self.AGENT_SANDBOX_MAX_OUTPUT_BYTES = int(
            _get_env("AGENT_SANDBOX_MAX_OUTPUT_BYTES")
        )
        self.DAYTONA_API_KEY = str(_get_env("DAYTONA_API_KEY") or "")
        self.DAYTONA_API_URL = str(
            _get_env("DAYTONA_API_URL") or "https://app.daytona.io/api"
        ).rstrip("/")
        self.BUILTIN_SKILLS_ROOT = _get_env("BUILTIN_SKILLS_ROOT")
        self.WORKSPACE_MAX_BYTES = _get_workspace_limit("WORKSPACE_MAX_BYTES")
        self.WORKSPACE_MAX_FILES = _get_workspace_limit("WORKSPACE_MAX_FILES")
        self.WORKSPACE_PREVIEW_MAX_BYTES = _get_workspace_limit(
            "WORKSPACE_PREVIEW_MAX_BYTES"
        )
        self.WORKSPACE_DOWNLOAD_URL_TTL_SECONDS = _get_workspace_limit(
            "WORKSPACE_DOWNLOAD_URL_TTL_SECONDS"
        )
        self.AGENT_INTERNAL_PROTOCOL_BASE_URL = str(
            _get_env("AGENT_INTERNAL_PROTOCOL_BASE_URL")
            or "http://127.0.0.1:5000/agent-protocol"
        ).rstrip("/")
        self.AGENT_INTERNAL_PROTOCOL_SECRET = str(
            _get_env("AGENT_INTERNAL_PROTOCOL_SECRET")
            or os.getenv("JWT_SECRET_KEY", "")
        )
        self.AGENT_INTERNAL_PROTOCOL_TOKEN_TTL_SECONDS = int(
            _get_env("AGENT_INTERNAL_PROTOCOL_TOKEN_TTL_SECONDS")
        )
        if self.AGENT_INTERNAL_PROTOCOL_TOKEN_TTL_SECONDS < 60:
            raise ValueError(
                "AGENT_INTERNAL_PROTOCOL_TOKEN_TTL_SECONDS 必须至少为 60 秒"
            )
        self._validate_database_settings()
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
        self.REDIS_HEALTH_CHECK_INTERVAL = int(_get_env("REDIS_HEALTH_CHECK_INTERVAL"))

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

        # 腾讯云 COS 对象存储
        self.COS_SECRET_ID = _get_env("COS_SECRET_ID")
        self.COS_SECRET_KEY = _get_env("COS_SECRET_KEY")
        self.COS_REGION = _get_env("COS_REGION")
        self.COS_SCHEME = _get_env("COS_SCHEME")
        self.COS_BUCKET = _get_env("COS_BUCKET")

    def _validate_database_settings(self) -> None:
        """校验 ORM 和 PostgreSQL 连接池参数。"""
        if self.SQLALCHEMY_POOL_SIZE <= 0:
            raise ValueError("SQLALCHEMY_POOL_SIZE 必须大于 0")
        if self.SQLALCHEMY_MAX_OVERFLOW < 0:
            raise ValueError("SQLALCHEMY_MAX_OVERFLOW 不能小于 0")
        if self.SQLALCHEMY_POOL_TIMEOUT_SECONDS <= 0:
            raise ValueError("SQLALCHEMY_POOL_TIMEOUT_SECONDS 必须大于 0")
        if self.SQLALCHEMY_POOL_RECYCLE <= 0:
            raise ValueError("SQLALCHEMY_POOL_RECYCLE 必须大于 0")
        positive_settings = (
            "POSTGRES_CONNECT_TIMEOUT_SECONDS",
            "POSTGRES_KEEPALIVES_IDLE_SECONDS",
            "POSTGRES_KEEPALIVES_INTERVAL_SECONDS",
            "POSTGRES_KEEPALIVES_COUNT",
            "POSTGRES_POOL_MAX_LIFETIME_SECONDS",
            "POSTGRES_POOL_MAX_IDLE_SECONDS",
            "POSTGRES_POOL_RECONNECT_TIMEOUT_SECONDS",
        )
        for name in positive_settings:
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 必须大于 0")
        if self.POSTGRES_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS <= 0:
            raise ValueError("POSTGRES_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS 必须大于 0")

    def _validate_agent_settings(self) -> None:
        """校验 Agent 持久化和记忆配置。"""
        if self.AGENT_RUN_MAX_WORKERS <= 0:
            raise ValueError("AGENT_RUN_MAX_WORKERS 必须大于 0")
        supported = {"postgres", "memory", "disabled", "false", "off", "none"}
        if self.AGENT_CHECKPOINT_BACKEND not in supported:
            raise ValueError(
                "AGENT_CHECKPOINT_BACKEND 必须是 postgres、memory 或 disabled"
            )
        if self.AGENT_CHECKPOINT_POOL_MIN_SIZE < 0:
            raise ValueError("AGENT_CHECKPOINT_POOL_MIN_SIZE 不能小于 0")
        if (
            self.AGENT_CHECKPOINT_POOL_MAX_SIZE <= 0
            or self.AGENT_CHECKPOINT_POOL_MAX_SIZE < self.AGENT_CHECKPOINT_POOL_MIN_SIZE
        ):
            raise ValueError("AGENT_CHECKPOINT_POOL_MAX_SIZE 必须大于等于 MIN_SIZE")
        if self.AGENT_CHECKPOINT_POOL_TIMEOUT_SECONDS <= 0:
            raise ValueError("AGENT_CHECKPOINT_POOL_TIMEOUT_SECONDS 必须大于 0")
        if self.AGENT_CHECKPOINT_POOL_MAX_WAITING < 0:
            raise ValueError("AGENT_CHECKPOINT_POOL_MAX_WAITING 不能小于 0")
        if self.AGENT_MEMORY_BACKEND not in {"postgres", "memory"}:
            raise ValueError(
                "AGENT_MEMORY_BACKEND 不允许关闭，必须是 postgres 或 memory"
            )
        if self.AGENT_MEMORY_POOL_MIN_SIZE < 0:
            raise ValueError("AGENT_MEMORY_POOL_MIN_SIZE 不能小于 0")
        if (
            self.AGENT_MEMORY_POOL_MAX_SIZE <= 0
            or self.AGENT_MEMORY_POOL_MAX_SIZE < self.AGENT_MEMORY_POOL_MIN_SIZE
        ):
            raise ValueError("AGENT_MEMORY_POOL_MAX_SIZE 必须大于等于 MIN_SIZE")
        if self.AGENT_MEMORY_POOL_TIMEOUT_SECONDS <= 0:
            raise ValueError("AGENT_MEMORY_POOL_TIMEOUT_SECONDS 必须大于 0")
        if self.AGENT_MEMORY_POOL_MAX_WAITING < 0:
            raise ValueError("AGENT_MEMORY_POOL_MAX_WAITING 不能小于 0")
        if (
            os.getenv("FLASK_ENV", "").lower() == "production"
            and self.AGENT_CHECKPOINT_BACKEND != "postgres"
        ):
            raise ValueError("生产环境必须使用 postgres checkpoint")
        if self.AGENT_SANDBOX_PROVIDER not in {
            "disabled",
            "bubblewrap",
            "bwrap",
            "langsmith",
            "daytona",
            "local_shell",
        }:
            raise ValueError(
                "AGENT_SANDBOX_PROVIDER 必须是 disabled、bubblewrap、langsmith、daytona 或 local_shell"
            )
        if (
            os.getenv("FLASK_ENV", "").lower() == "production"
            and self.AGENT_SANDBOX_PROVIDER == "local_shell"
        ):
            raise ValueError(
                "生产环境禁止使用 local_shell，请配置 bubblewrap、langsmith 或 daytona"
            )
        if (
            os.getenv("FLASK_ENV", "").lower() == "production"
            and self.AGENT_SANDBOX_PROVIDER == "daytona"
            and not self.DAYTONA_API_KEY
        ):
            raise ValueError("生产环境启用 Daytona Sandbox 时必须配置 DAYTONA_API_KEY")
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
        if self.AGENT_SANDBOX_EXECUTE_TIMEOUT_SECONDS <= 0:
            raise ValueError("AGENT_SANDBOX_EXECUTE_TIMEOUT_SECONDS 必须大于 0")
        if self.AGENT_SANDBOX_MAX_OUTPUT_BYTES <= 0:
            raise ValueError("AGENT_SANDBOX_MAX_OUTPUT_BYTES 必须大于 0")
