from typing import Any, Optional

from psycopg2 import OperationalError as Psycopg2OperationalError
from sqlalchemy.exc import OperationalError

from internal.exception import FailException
from internal.extension.logging_extension import logger
from pkg.sqlalchemy import SQLAlchemy


class BaseService:
    """基础服务，完善数据库的基础增删改查功能，简化代码"""

    db: SQLAlchemy

    def create(self, model: Any, **kwargs) -> Any:
        """根据传递的模型类+键值对信息创建数据库记录"""
        with self.db.auto_commit():
            model_instance = model(**kwargs)
            self.db.session.add(model_instance)
            self.db.session.flush()
        return model_instance

    def delete(self, model_instance: Any) -> Any:
        """根据传递的模型实例删除数据库记录"""
        with self.db.auto_commit():
            self.db.session.delete(model_instance)
        return model_instance

    def update(self, model_instance: Any, **kwargs) -> Any:
        """根据传递的模型实例+键值对信息更新数据库记录"""
        with self.db.auto_commit():
            for field, value in kwargs.items():
                if hasattr(model_instance, field):
                    setattr(model_instance, field, value)
                else:
                    raise FailException("更新数据失败")
        return model_instance

    def get(self, model: Any, primary_key: Any) -> Optional[Any]:
        """根据传递的模型类+主键的信息获取唯一数据"""
        try:
            return self.db.session.query(model).get(primary_key)
        except (OperationalError, Psycopg2OperationalError) as e:
            logger.exception(
                "Database connection error when getting %s with id %s: %s",
                model.__name__,
                primary_key,
                e,
            )
            # 尝试刷新会话连接
            try:
                self.db.session.rollback()
                return self.db.session.query(model).get(primary_key)
            except (OperationalError, Psycopg2OperationalError):
                # 如果重试仍然失败，抛出自定义异常
                raise FailException("数据库连接异常，请稍后重试")
