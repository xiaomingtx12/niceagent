"""
站点自定义配置
在 Python 启动时自动加载，用于配置全局警告过滤器
"""
import warnings

# 过滤 jieba 相关的警告
warnings.filterwarnings('ignore', category=UserWarning, module='jieba')
warnings.filterwarnings('ignore', category=ResourceWarning, module='jieba')
warnings.filterwarnings('ignore', message='pkg_resources is deprecated')

# 过滤 Pydantic 和 Marshmallow 的弃用警告
warnings.filterwarnings('ignore', message="Support for class-based `config` is deprecated")
warnings.filterwarnings('ignore', message="The 'default' argument to fields is deprecated")
