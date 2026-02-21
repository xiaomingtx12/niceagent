"""Weaviate向量数据库扩展"""
from flask_weaviate import FlaskWeaviate

# 创建FlaskWeaviate实例，不使用嵌入式模式
# 配置将从Flask app.config中读取
weaviate = FlaskWeaviate()
