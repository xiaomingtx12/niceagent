#!/usr/bin/env python
"""
WSGI 入口文件
用于生产环境部署（gunicorn/uwsgi）
"""
import warnings
import sys
import os

# 必须在导入任何第三方库之前设置警告过滤
warnings.filterwarnings('ignore', category=UserWarning, module='jieba')
warnings.filterwarnings('ignore', category=ResourceWarning, module='jieba')
warnings.filterwarnings('ignore', message='pkg_resources is deprecated')
warnings.filterwarnings('ignore', message="Support for class-based `config` is deprecated")
warnings.filterwarnings('ignore', message="The 'default' argument to fields is deprecated")

# 设置 Python 路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.http.app import app

# WSGI 应用对象
application = app

if __name__ == '__main__':
    app.run()
