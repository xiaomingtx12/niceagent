"""
应用启动入口
在导入任何模块之前先配置警告过滤器
"""
import warnings
import sys

# 必须在导入任何第三方库之前设置警告过滤
warnings.filterwarnings('ignore', category=UserWarning, module='jieba')
warnings.filterwarnings('ignore', category=ResourceWarning, module='jieba')
warnings.filterwarnings('ignore', message='pkg_resources is deprecated')
warnings.filterwarnings('ignore', message="Support for class-based `config` is deprecated")
warnings.filterwarnings('ignore', message="The 'default' argument to fields is deprecated")

import dotenv
from flask_login import LoginManager
from flask_migrate import Migrate

from app.http.module import injector
from config import Config
from internal.middleware import Middleware
from internal.router import Router
from internal.server import Http
from pkg.sqlalchemy import SQLAlchemy
from flask_weaviate import FlaskWeaviate

# 将env加载到环境变量中
dotenv.load_dotenv()


app = Http(
    __name__,
    conf=injector.get(Config),
    db=injector.get(SQLAlchemy),
    migrate=injector.get(Migrate),
    router=injector.get(Router),
    login_manager=injector.get(LoginManager),
    weaviate=injector.get(FlaskWeaviate),
    middleware=injector.get(Middleware),
)

celery = app.extensions["celery"]

if __name__ == "__main__":
    app.run(debug=True)
