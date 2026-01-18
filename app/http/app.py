import dotenv
from flask_login import LoginManager
from flask_migrate import Migrate

from app.http.module import injector
from config import Config
from internal.middleware import Middleware
from internal.router import Router
from internal.server import Http
from pkg.sqlalchemy import SQLAlchemy

# 将env加载到环境变量中
dotenv.load_dotenv()


app = Http(
    __name__,
    conf=injector.get(Config),
    db=injector.get(SQLAlchemy),
    migrate=injector.get(Migrate),
    router=injector.get(Router),
    login_manager=injector.get(LoginManager),
    middleware=injector.get(Middleware),
)

celery = app.extensions["celery"]

if __name__ == "__main__":
    app.run(debug=True)
