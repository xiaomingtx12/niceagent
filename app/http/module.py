from flask_login import LoginManager
from flask_migrate import Migrate
from injector import Binder, Injector, Module, provider, singleton
from redis import Redis
from flask_weaviate import FlaskWeaviate

from config import Config
from internal.extension.database_extension import db
from internal.extension.login_extension import login_manager
from internal.extension.migrate_extension import migrate
from internal.extension.redis_extension import redis_client
from internal.extension.weaviate_extension import weaviate
from internal.repository import (
    AgentWorkbenchRedisRepository,
    BuiltinSkillRepository,
    CosObjectStorageRepository,
    WorkspaceFileStore,
)
from internal.service.agent_workbench_event_queue import (
    AgentWorkbenchEventQueueManager,
)
from internal.service.agent_workbench_execution_service import (
    AgentWorkbenchRunExecutionService,
)
from internal.service.agent_workbench_run_coordinator import (
    AgentWorkbenchRunCoordinator,
)
from internal.service.agent_workbench_run_factory import AgentWorkbenchRunFactory
from internal.service.agent_workbench_service import AgentWorkbenchService
from internal.service.embeddings_service import EmbeddingsService
from internal.service.faiss_service import FaissService
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
        binder.bind(
            AgentWorkbenchService,
            to=AgentWorkbenchService,
            scope=singleton,
        )
        # Workbench 的生产端和订阅端共享同一个进程级队列管理器。
        binder.bind(
            AgentWorkbenchEventQueueManager,
            to=AgentWorkbenchEventQueueManager,
            scope=singleton,
        )
        binder.bind(
            AgentWorkbenchRunFactory,
            to=AgentWorkbenchRunFactory,
            scope=singleton,
        )
        binder.bind(
            AgentWorkbenchRunExecutionService,
            to=AgentWorkbenchRunExecutionService,
            scope=singleton,
        )
        binder.bind(
            AgentWorkbenchRunCoordinator,
            to=AgentWorkbenchRunCoordinator,
            scope=singleton,
        )
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

    @singleton
    @provider
    def provide_agent_workbench_redis_repository(
        self,
        redis_client: Redis,
        config: Config,
    ) -> AgentWorkbenchRedisRepository:
        """使用统一配置构造进程级工作台 Redis Repository。"""
        return AgentWorkbenchRedisRepository(
            redis_client,
            key_prefix=config.AGENT_WORKBENCH_REPLAY_KEY_PREFIX,
            ttl_seconds=config.AGENT_WORKBENCH_REPLAY_TTL_SECONDS,
            max_length=config.AGENT_WORKBENCH_REPLAY_MAX_LENGTH,
        )


injector = Injector([ExtensionModule])
