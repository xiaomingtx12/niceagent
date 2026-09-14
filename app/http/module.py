from flask_login import LoginManager
from flask_migrate import Migrate
from injector import Binder, Injector, Module, singleton
from redis import Redis
from flask_weaviate import FlaskWeaviate

from config import Config
from internal.extension.database_extension import db
from internal.extension.login_extension import login_manager
from internal.extension.migrate_extension import migrate
from internal.extension.redis_extension import redis_client
from internal.extension.weaviate_extension import weaviate
from internal.repository import (
    BuiltinSkillRepository,
    CosObjectStorageRepository,
    WorkspaceFileStore,
)
from internal.service.agent.agent_run_events import AgentRunTracker
from internal.service.agent.agent_run_service import AgentRunService
from internal.service.dataset.embeddings_service import EmbeddingsService
from internal.service.dataset.faiss_service import FaissService
from pkg.sqlalchemy import SQLAlchemy


class ExtensionModule(Module):
    """扩展模块的依赖注入"""

    def configure(self, binder: Binder) -> None:
        binder.bind(Config, to=Config, scope=singleton)
        binder.bind(SQLAlchemy, to=db)
        binder.bind(Migrate, to=migrate)
        binder.bind(Redis, to=redis_client)
        binder.bind(LoginManager, to=login_manager)
        binder.bind(FlaskWeaviate, to=weaviate)
        binder.bind(AgentRunService, to=AgentRunService, scope=singleton)
        # Run 的生产端和订阅端共享同一个进程级跟踪器。
        binder.bind(AgentRunTracker, to=AgentRunTracker, scope=singleton)
        # 将 EmbeddingsService 和 FaissService 绑定为单例，确保所有地方使用同一个实例
        binder.bind(EmbeddingsService, to=EmbeddingsService, scope=singleton)
        binder.bind(FaissService, to=FaissService, scope=singleton)
        binder.bind(
            BuiltinSkillRepository,
            to=BuiltinSkillRepository,
            scope=singleton,
        )
        binder.bind(
            CosObjectStorageRepository,
            to=CosObjectStorageRepository,
            scope=singleton,
        )
        binder.bind(
            WorkspaceFileStore,
            to=WorkspaceFileStore,
            scope=singleton,
        )


injector = Injector([ExtensionModule])
