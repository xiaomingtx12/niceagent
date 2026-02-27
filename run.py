#!/usr/bin/env python
"""
Flask 应用启动脚本
在导入任何模块之前配置警告过滤器
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

if __name__ == '__main__':
    from app.http.app import app
    app.run(debug=True, host='0.0.0.0', port=5000)
