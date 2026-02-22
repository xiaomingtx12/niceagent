from flask_login import LoginManager
from flask_migrate import Migrate
from injector import Module, Binder, Injector, singleton
from redis import Redis
from flask_weaviate import FlaskWeaviate

from internal.extension.database_extension import db
from internal.extension.login_extension import login_manager
from internal.extension.migrate_extension import migrate
from internal.extension.redis_extension import redis_client
from internal.extension.weaviate_extension import weaviate
from internal.service.embeddings_service import EmbeddingsService
from internal.service.faiss_service import FaissService
from pkg.sqlalchemy import SQLAlchemy


class ExtensionModule(Module):
    """扩展模块的依赖注入"""

    def configure(self, binder: Binder) -> None:
        binder.bind(SQLAlchemy, to=db)
        binder.bind(Migrate, to=migrate)
        binder.bind(Redis, to=redis_client)
        binder.bind(LoginManager, to=login_manager)
        binder.bind(FlaskWeaviate, to=weaviate)
        # 将 EmbeddingsService 和 FaissService 绑定为单例，确保所有地方使用同一个实例
        binder.bind(EmbeddingsService, to=EmbeddingsService, scope=singleton)
        binder.bind(FaissService, to=FaissService, scope=singleton)


injector = Injector([ExtensionModule])
