import dotenv
from injector import Injector

from config import Config
from internal.router import Router
from internal.server import Http

# 将env加载到环境变量中
dotenv.load_dotenv()

injector = Injector()
app = Http(
    __name__,
    conf=injector.get(Config),
    router=injector.get(Router),
)

if __name__ == "__main__":
    app.run(debug=True)
