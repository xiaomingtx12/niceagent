import os

from flask import Flask
from flask_cors import CORS
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_weaviate import FlaskWeaviate
from psycopg import OperationalError as PsycopgOperationalError
from psycopg2 import OperationalError as Psycopg2OperationalError
from sqlalchemy.exc import OperationalError

from config import Config
from internal.exception import CustomException, TooManyRequestsException
from internal.extension import redis_extension, celery_extension, logging_extension
from internal.extension.logging_extension import logger
from internal.middleware import Middleware
from internal.middleware.rate_limit_middleware import init_rate_limit_middleware
from internal.router import Router
from pkg.response import json, Response, HttpCode, too_many_requests_message
from pkg.sqlalchemy import SQLAlchemy


class Http(Flask):

    def __init__(
        self,
        *args,
        conf: Config,
        db: SQLAlchemy,
        migrate: Migrate,
        login_manager: LoginManager,
        weaviate: FlaskWeaviate,
        middleware: Middleware,
        router: Router,
        **kwargs,
    ):
        # 1.调用父类构造函数初始化
        super(Http, self).__init__(*args, **kwargs)

        # 2.初始化应用配置
        self.config.from_object(conf)

        # 3.注册绑定异常错误处理
        self.register_error_handler(Exception, self._register_error_handler)
        self.register_error_handler(
            TooManyRequestsException, self._handle_rate_limit_error
        )
        self.register_error_handler(
            OperationalError, self._handle_database_operational_error
        )
        self.register_error_handler(
            Psycopg2OperationalError, self._handle_database_operational_error
        )
        self.register_error_handler(
            PsycopgOperationalError, self._handle_database_operational_error
        )
        self.register_error_handler(404, self._handle_not_found_error)

        # 4.初始化flask扩展
        db.init_app(self)
        migrate.init_app(
            self,
            db,
            directory="internal/migration",
        )
        redis_extension.init_app(self)
        celery_extension.init_app(self)
        logging_extension.init_app(self)
        login_manager.init_app(self)
        weaviate.init_app(self)

        # 5.解决前后端跨域问题
        CORS(
            self,
            resources={
                r"/*": {
                    "origins": "*",
                    "supports_credentials": True,
                    # "methods": ["GET", "POST"],
                    # "allow_headers": ["Content-Type"],
                }
            },
        )
        # 6.注册应用中间件
        login_manager.request_loader(middleware.request_loader)
        
        # 7.初始化限流中间件
        init_rate_limit_middleware(self)

        # 8.注册应用路由
        router.register_router(self)

    def _register_error_handler(self, error: Exception):
        # Flask 在部分异常链路中会优先命中 Exception 处理器，不能只依赖
        # 具体异常类注册，数据库连接错误必须始终返回可消费的业务响应。
        if isinstance(
            error,
            (
                OperationalError,
                Psycopg2OperationalError,
                PsycopgOperationalError,
            ),
        ):
            return self._handle_database_operational_error(error)

        # 1.异常信息是不是我们的自定义异常，如果是可以提取message和code等信息
        if isinstance(error, CustomException):
            return json(
                Response(
                    code=error.code,
                    message=error.message,
                    data=error.data if error.data is not None else {},
                )
            )
        # 2.未知异常仅在服务端保留详情，避免向客户端泄露内部信息。
        if self.debug or os.getenv("FLASK_ENV") == "development":
            raise error
        logger.error(
            "Unhandled application exception",
            exc_info=(type(error), error, error.__traceback__),
        )
        return json(
            Response(
                code=HttpCode.FAIL,
                message="服务器内部错误，请稍后重试",
                data={},
            )
        )

    def _handle_not_found_error(self, error):
        """处理404 Not Found错误"""
        return json(
            Response(
                code=HttpCode.NOT_FOUND,
                message="The requested resource was not found",
                data={},
            )
        )

    def _handle_database_operational_error(self, error: Exception):
        """处理数据库操作异常"""
        logger.error("Database operational error: %s", error)
        return json(
            Response(
                code=HttpCode.FAIL,
                message="数据库连接异常，请稍后重试",
                data={},
            )
        )

    def _handle_rate_limit_error(self, error: TooManyRequestsException):
        """处理限流异常"""
        logger.warning("Rate limit exceeded: %s", error.message)
        return too_many_requests_message(
            msg=error.message,
            retry_after=error.retry_after
        )
