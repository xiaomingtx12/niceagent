"""
API 限流中间件

提供装饰器和中间件函数，用于保护 API 端点
"""
import logging
from functools import wraps
from typing import Optional, Callable, Union

from flask import request, g, current_app
from redis import Redis

from internal.exception import TooManyRequestsException
from internal.lib.rate_limiter import (
    RateLimiter,
    RateLimitConfig,
    RateLimitStrategy,
    RateLimitResult
)


logger = logging.getLogger(__name__)


# 默认限流配置
# 针对 4核4G 轻量级服务器优化的配置
DEFAULT_RATE_LIMITS = {
    # 全局限流 (所有 API) - 保护服务器整体负载
    # 4核服务器建议: 300-500 请求/分钟
    "global": RateLimitConfig(
        max_requests=400,
        window_seconds=60,
        strategy=RateLimitStrategy.FIXED_WINDOW  # 使用固定窗口减少内存占用
    ),
    
    # 用户级别限流 (已登录用户)
    # 单用户建议: 30-50 请求/分钟
    "user": RateLimitConfig(
        max_requests=40,
        window_seconds=60,
        strategy=RateLimitStrategy.SLIDING_WINDOW
    ),
    
    # IP 级别限流 (未登录用户) - 更严格防止滥用
    # 单IP建议: 20-30 请求/分钟
    "ip": RateLimitConfig(
        max_requests=20,
        window_seconds=60,
        strategy=RateLimitStrategy.SLIDING_WINDOW
    ),
    
    # 对话接口限流 (消耗 LLM 资源和计算资源)
    # LLM调用建议: 10-15 请求/分钟
    "chat": RateLimitConfig(
        max_requests=12,
        window_seconds=60,
        strategy=RateLimitStrategy.TOKEN_BUCKET  # 允许短时突发
    ),
    
    # 文件上传限流 (消耗 IO 和内存)
    # 上传建议: 5-8 请求/分钟
    "upload": RateLimitConfig(
        max_requests=6,
        window_seconds=60,
        strategy=RateLimitStrategy.FIXED_WINDOW
    ),
    
    # 知识库检索限流 (消耗向量数据库资源)
    # 检索建议: 15-20 请求/分钟
    "retrieval": RateLimitConfig(
        max_requests=18,
        window_seconds=60,
        strategy=RateLimitStrategy.SLIDING_WINDOW
    ),
    
    # 工作流执行限流 (消耗多种资源)
    # 工作流建议: 8-12 请求/分钟
    "workflow": RateLimitConfig(
        max_requests=10,
        window_seconds=60,
        strategy=RateLimitStrategy.TOKEN_BUCKET
    ),
    
    # 敏感操作限流 (删除、发布等)
    # 敏感操作建议: 3-5 请求/分钟
    "sensitive": RateLimitConfig(
        max_requests=3,
        window_seconds=60,
        strategy=RateLimitStrategy.FIXED_WINDOW
    ),
    
    # 数据库密集型操作限流 (列表查询、统计等)
    "database_intensive": RateLimitConfig(
        max_requests=25,
        window_seconds=60,
        strategy=RateLimitStrategy.SLIDING_WINDOW
    ),
    
    # 轻量级操作限流 (获取单个资源详情等)
    "lightweight": RateLimitConfig(
        max_requests=60,
        window_seconds=60,
        strategy=RateLimitStrategy.FIXED_WINDOW
    ),
}


def get_rate_limiter() -> Optional[RateLimiter]:
    """
    获取限流器实例
    
    Returns:
        RateLimiter: 限流器实例，如果 Redis 不可用则返回 None
    """
    try:
        redis_client: Redis = current_app.extensions.get("redis")
        if redis_client:
            return RateLimiter(redis_client)
    except Exception as e:
        logger.warning(f"获取限流器失败: {str(e)}")
    return None


def get_rate_limit_key(
    identifier: str,
    scope: str = "api"
) -> str:
    """
    生成限流键
    
    Args:
        identifier: 标识符 (用户ID、IP地址等)
        scope: 限流范围 (api, chat, upload 等)
        
    Returns:
        str: 限流键
    """
    return f"rate_limit:{scope}:{identifier}"


def get_client_identifier() -> str:
    """
    获取客户端标识符
    
    优先级:
    1. 已登录用户: user:{user_id}
    2. API Key: apikey:{api_key_id}
    3. IP 地址: ip:{ip_address}
    
    Returns:
        str: 客户端标识符
    """
    # 1. 检查是否有已登录用户
    if hasattr(g, "current_user") and g.current_user:
        return f"user:{g.current_user.id}"
    
    # 2. 检查是否使用 API Key
    if hasattr(g, "api_key") and g.api_key:
        return f"apikey:{g.api_key.id}"
    
    # 3. 使用 IP 地址
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if ip:
        # 如果有多个 IP (经过多个代理)，取第一个
        ip = ip.split(",")[0].strip()
    else:
        ip = "unknown"
    
    return f"ip:{ip}"


def check_rate_limit(
    config: RateLimitConfig,
    scope: str = "api",
    identifier: Optional[str] = None
) -> RateLimitResult:
    """
    检查限流
    
    Args:
        config: 限流配置
        scope: 限流范围
        identifier: 自定义标识符 (可选)
        
    Returns:
        RateLimitResult: 限流结果
        
    Raises:
        TooManyRequestsException: 超过限流时抛出
    """
    limiter = get_rate_limiter()
    
    # 如果限流器不可用，默认允许请求
    if not limiter:
        logger.warning("限流器不可用，跳过限流检查")
        return RateLimitResult(
            allowed=True,
            remaining=config.max_requests,
            reset_at=0
        )
    
    # 获取客户端标识符
    if identifier is None:
        identifier = get_client_identifier()
    
    # 生成限流键
    key = get_rate_limit_key(identifier, scope)
    
    # 执行限流检查
    result = limiter.check_rate_limit(key, config)
    
    # 记录限流信息到响应头
    g.rate_limit_result = result
    
    # 如果超过限流，抛出异常
    if not result.allowed:
        logger.warning(
            f"限流触发: scope={scope}, identifier={identifier}, "
            f"limit={config.max_requests}/{config.window_seconds}s"
        )
        raise TooManyRequestsException(
            message=f"请求过于频繁，请在 {result.retry_after} 秒后重试",
            retry_after=result.retry_after
        )
    
    return result


def rate_limit(
    max_requests: Optional[int] = None,
    window_seconds: Optional[int] = None,
    strategy: Optional[RateLimitStrategy] = None,
    scope: str = "api",
    config_key: Optional[str] = None
):
    """
    限流装饰器
    
    使用方式:
    
    1. 使用预定义配置:
        @rate_limit(config_key="chat")
        def chat_endpoint():
            pass
    
    2. 自定义配置:
        @rate_limit(max_requests=10, window_seconds=60, scope="custom")
        def custom_endpoint():
            pass
    
    3. 组合使用:
        @rate_limit(config_key="user")  # 用户级别限流
        @rate_limit(config_key="chat")  # 对话接口限流
        def chat_endpoint():
            pass
    
    Args:
        max_requests: 最大请求数 (可选)
        window_seconds: 时间窗口秒数 (可选)
        strategy: 限流策略 (可选)
        scope: 限流范围
        config_key: 预定义配置键 (可选)
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 1. 确定使用的配置
            if config_key and config_key in DEFAULT_RATE_LIMITS:
                config = DEFAULT_RATE_LIMITS[config_key]
            elif max_requests and window_seconds:
                config = RateLimitConfig(
                    max_requests=max_requests,
                    window_seconds=window_seconds,
                    strategy=strategy or RateLimitStrategy.SLIDING_WINDOW
                )
            else:
                # 使用默认配置
                config = DEFAULT_RATE_LIMITS.get("user", DEFAULT_RATE_LIMITS["global"])
            
            # 2. 执行限流检查
            check_rate_limit(config, scope)
            
            # 3. 执行原函数
            return func(*args, **kwargs)
        
        return wrapper
    return decorator


def add_rate_limit_headers(response):
    """
    添加限流相关的响应头
    
    响应头:
    - X-RateLimit-Limit: 限流上限
    - X-RateLimit-Remaining: 剩余请求数
    - X-RateLimit-Reset: 重置时间戳
    - Retry-After: 建议重试时间 (仅在限流时)
    """
    if hasattr(g, "rate_limit_result"):
        result: RateLimitResult = g.rate_limit_result
        
        # 添加标准限流响应头
        response.headers["X-RateLimit-Limit"] = str(
            result.remaining + (0 if result.allowed else 1)
        )
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)
        response.headers["X-RateLimit-Reset"] = str(result.reset_at)
        
        # 如果被限流，添加 Retry-After 头
        if not result.allowed and result.retry_after:
            response.headers["Retry-After"] = str(result.retry_after)
    
    return response


def init_rate_limit_middleware(app):
    """
    初始化限流中间件
    
    Args:
        app: Flask 应用实例
    """
    # 注册响应后处理器，添加限流响应头
    app.after_request(add_rate_limit_headers)
    
    logger.info("限流中间件已初始化")
