from dataclasses import dataclass

from flask import Flask, Blueprint
from injector import inject

from internal.handler import AppHandler


@inject
@dataclass
class Router(object):
    "" "路由 " ""
    app_handler = AppHandler

    def __init__(self, app_handler: AppHandler):
        self.app_handler = app_handler

    def register_router(self, app: Flask):
        """注册路由"""
        # 1.创建一个蓝图（一组路由的集合/前台路由/后台路由）
        bp = Blueprint("router", __name__, url_prefix="")
        # 2.将url与对应的控制器方法做绑定
        app_handler = AppHandler()
        bp.add_url_rule("/ping", methods=["GET"], view_func=self.app_handler.ping)
        # 3.在应用上去注册蓝图
        app.register_blueprint(bp)
