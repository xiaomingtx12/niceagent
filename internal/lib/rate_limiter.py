"""
API 限流器 - 基于 Redis 的分布式限流实现

支持多种限流策略:
1. 固定窗口 (Fixed Window)
2. 滑动窗口 (Sliding Window)
3. 令牌桶 (Token Bucket)
"""
import time
from typing import Optional, Tuple
from enum import Enum
from dataclasses import dataclass

from redis import Redis
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
    TimeoutError as RedisTimeoutError,
)

from internal.extension.logging_extension import logger

_REDIS_CONNECTION_ERRORS = (RedisConnectionError, RedisTimeoutError)


class RateLimitStrategy(str, Enum):
    """限流策略枚举"""
    FIXED_WINDOW = "fixed_window"  # 固定窗口
    SLIDING_WINDOW = "sliding_window"  # 滑动窗口
    TOKEN_BUCKET = "token_bucket"  # 令牌桶


@dataclass
class RateLimitConfig:
    """限流配置"""
    max_requests: int  # 最大请求数
    window_seconds: int  # 时间窗口(秒)
    strategy: RateLimitStrategy = RateLimitStrategy.SLIDING_WINDOW
    
    def __post_init__(self):
        """验证配置"""
        if self.max_requests <= 0:
            raise ValueError("max_requests 必须大于 0")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds 必须大于 0")


@dataclass
class RateLimitResult:
    """限流结果"""
    allowed: bool  # 是否允许请求
    remaining: int  # 剩余请求数
    reset_at: int  # 重置时间戳
    retry_after: Optional[int] = None  # 建议重试时间(秒)


class RateLimiter:
    """分布式限流器"""
    
    def __init__(self, redis_client: Redis):
        """
        初始化限流器
        
        Args:
            redis_client: Redis 客户端实例
        """
        self.redis = redis_client

    def _handle_backend_error(self, operation: str, error: Exception) -> None:
        """清理失效连接并记录限流降级。"""
        if isinstance(error, _REDIS_CONNECTION_ERRORS):
            try:
                self.redis.connection_pool.disconnect(inuse_connections=True)
            except Exception:
                pass
            logger.warning(
                "Redis %s暂时不可用，已放行本次请求: %s",
                operation,
                error,
            )
            return
        logger.error("Redis %s失败: %s", operation, error)
        
    def check_rate_limit(
        self,
        key: str,
        config: RateLimitConfig
    ) -> RateLimitResult:
        """
        检查是否超过限流
        
        Args:
            key: 限流键 (如: "rate_limit:user:123:api")
            config: 限流配置
            
        Returns:
            RateLimitResult: 限流结果
        """
        if config.strategy == RateLimitStrategy.FIXED_WINDOW:
            return self._fixed_window(key, config)
        elif config.strategy == RateLimitStrategy.SLIDING_WINDOW:
            return self._sliding_window(key, config)
        elif config.strategy == RateLimitStrategy.TOKEN_BUCKET:
            return self._token_bucket(key, config)
        else:
            raise ValueError(f"不支持的限流策略: {config.strategy}")
    
    def _fixed_window(
        self,
        key: str,
        config: RateLimitConfig
    ) -> RateLimitResult:
        """
        固定窗口限流算法
        
        优点: 实现简单，性能好
        缺点: 边界突刺问题 (窗口切换时可能瞬间超限)
        
        实现原理:
        1. 将时间划分为固定窗口 (如 60 秒)
        2. 每个窗口内计数请求数
        3. 超过限制则拒绝
        """
        now = int(time.time())
        window_start = now - (now % config.window_seconds)
        window_key = f"{key}:{window_start}"
        
        try:
            # 使用 Redis 管道提高性能
            pipe = self.redis.pipeline()
            pipe.incr(window_key)
            pipe.expire(window_key, config.window_seconds * 2)  # 设置过期时间
            results = pipe.execute()
            
            current_count = results[0]
            remaining = max(0, config.max_requests - current_count)
            reset_at = window_start + config.window_seconds
            
            allowed = current_count <= config.max_requests
            retry_after = None if allowed else (reset_at - now)
            
            return RateLimitResult(
                allowed=allowed,
                remaining=remaining,
                reset_at=reset_at,
                retry_after=retry_after
            )
            
        except Exception as e:
            self._handle_backend_error("固定窗口限流检查", e)
            # 限流器故障时默认允许请求 (Fail Open)
            return RateLimitResult(
                allowed=True,
                remaining=config.max_requests,
                reset_at=now + config.window_seconds
            )
    
    def _sliding_window(
        self,
        key: str,
        config: RateLimitConfig
    ) -> RateLimitResult:
        """
        滑动窗口限流算法
        
        优点: 平滑限流，避免边界突刺
        缺点: 需要存储每个请求的时间戳，内存占用较高
        
        实现原理:
        1. 使用 Redis Sorted Set 存储请求时间戳
        2. Score 为时间戳，Value 为唯一标识
        3. 查询时只统计窗口内的请求
        """
        now = time.time()
        window_start = now - config.window_seconds
        
        try:
            # 使用 Lua 脚本保证原子性
            lua_script = """
            local key = KEYS[1]
            local now = tonumber(ARGV[1])
            local window_start = tonumber(ARGV[2])
            local max_requests = tonumber(ARGV[3])
            local window_seconds = tonumber(ARGV[4])
            
            -- 删除窗口外的旧数据
            redis.call('ZREMRANGEBYSCORE', key, 0, window_start)
            
            -- 获取当前窗口内的请求数
            local current_count = redis.call('ZCARD', key)
            
            -- 判断是否超限
            if current_count < max_requests then
                -- 添加当前请求
                redis.call('ZADD', key, now, now)
                redis.call('EXPIRE', key, window_seconds * 2)
                return {1, current_count + 1}
            else
                return {0, current_count}
            end
            """
            
            result = self.redis.eval(
                lua_script,
                1,
                key,
                now,
                window_start,
                config.max_requests,
                config.window_seconds
            )
            
            allowed = bool(result[0])
            current_count = result[1]
            remaining = max(0, config.max_requests - current_count)
            reset_at = int(now + config.window_seconds)
            
            # 如果被限流，计算最早的请求何时过期
            retry_after = None
            if not allowed:
                try:
                    # 获取最早的请求时间戳
                    oldest = self.redis.zrange(key, 0, 0, withscores=True)
                    if oldest:
                        oldest_time = oldest[0][1]
                        retry_after = int(oldest_time + config.window_seconds - now) + 1
                except Exception:
                    retry_after = config.window_seconds
            
            return RateLimitResult(
                allowed=allowed,
                remaining=remaining,
                reset_at=reset_at,
                retry_after=retry_after
            )
            
        except Exception as e:
            self._handle_backend_error("滑动窗口限流检查", e)
            # 限流器故障时默认允许请求
            return RateLimitResult(
                allowed=True,
                remaining=config.max_requests,
                reset_at=int(now + config.window_seconds)
            )
    
    def _token_bucket(
        self,
        key: str,
        config: RateLimitConfig
    ) -> RateLimitResult:
        """
        令牌桶限流算法
        
        优点: 允许突发流量，更灵活
        缺点: 实现相对复杂
        
        实现原理:
        1. 桶中存储令牌，最多存储 max_requests 个
        2. 以固定速率添加令牌 (max_requests / window_seconds)
        3. 每次请求消耗一个令牌
        4. 令牌不足则拒绝请求
        """
        now = time.time()
        
        try:
            # 使用 Lua 脚本实现令牌桶
            lua_script = """
            local key = KEYS[1]
            local now = tonumber(ARGV[1])
            local max_tokens = tonumber(ARGV[2])
            local refill_rate = tonumber(ARGV[3])
            local window_seconds = tonumber(ARGV[4])
            
            -- 获取当前令牌数和上次更新时间
            local bucket = redis.call('HMGET', key, 'tokens', 'last_update')
            local tokens = tonumber(bucket[1]) or max_tokens
            local last_update = tonumber(bucket[2]) or now
            
            -- 计算应该添加的令牌数
            local time_passed = now - last_update
            local tokens_to_add = time_passed * refill_rate
            tokens = math.min(max_tokens, tokens + tokens_to_add)
            
            -- 判断是否有足够的令牌
            if tokens >= 1 then
                -- 消耗一个令牌
                tokens = tokens - 1
                redis.call('HMSET', key, 'tokens', tokens, 'last_update', now)
                redis.call('EXPIRE', key, window_seconds * 2)
                return {1, math.floor(tokens)}
            else
                -- 令牌不足
                redis.call('HMSET', key, 'tokens', tokens, 'last_update', now)
                redis.call('EXPIRE', key, window_seconds * 2)
                return {0, 0}
            end
            """
            
            # 计算令牌补充速率 (每秒补充多少个令牌)
            refill_rate = config.max_requests / config.window_seconds
            
            result = self.redis.eval(
                lua_script,
                1,
                key,
                now,
                config.max_requests,
                refill_rate,
                config.window_seconds
            )
            
            allowed = bool(result[0])
            remaining = result[1]
            reset_at = int(now + config.window_seconds)
            
            # 计算需要等待多久才能获得令牌
            retry_after = None
            if not allowed:
                retry_after = int(1 / refill_rate) + 1
            
            return RateLimitResult(
                allowed=allowed,
                remaining=remaining,
                reset_at=reset_at,
                retry_after=retry_after
            )
            
        except Exception as e:
            self._handle_backend_error("令牌桶限流检查", e)
            # 限流器故障时默认允许请求
            return RateLimitResult(
                allowed=True,
                remaining=config.max_requests,
                reset_at=int(now + config.window_seconds)
            )
    
    def reset(self, key: str) -> bool:
        """
        重置限流计数器
        
        Args:
            key: 限流键
            
        Returns:
            bool: 是否成功重置
        """
        try:
            # 删除所有相关的键
            pattern = f"{key}*"
            keys = self.redis.keys(pattern)
            if keys:
                self.redis.delete(*keys)
            return True
        except Exception as e:
            self._handle_backend_error("重置限流计数器", e)
            return False
    
    def get_current_usage(
        self,
        key: str,
        config: RateLimitConfig
    ) -> Tuple[int, int]:
        """
        获取当前使用情况
        
        Args:
            key: 限流键
            config: 限流配置
            
        Returns:
            Tuple[int, int]: (当前请求数, 最大请求数)
        """
        try:
            if config.strategy == RateLimitStrategy.SLIDING_WINDOW:
                now = time.time()
                window_start = now - config.window_seconds
                self.redis.zremrangebyscore(key, 0, window_start)
                current = self.redis.zcard(key)
                return (current, config.max_requests)
            elif config.strategy == RateLimitStrategy.TOKEN_BUCKET:
                bucket = self.redis.hmget(key, 'tokens')
                tokens = int(float(bucket[0])) if bucket[0] else config.max_requests
                return (config.max_requests - tokens, config.max_requests)
            else:  # FIXED_WINDOW
                now = int(time.time())
                window_start = now - (now % config.window_seconds)
                window_key = f"{key}:{window_start}"
                current = self.redis.get(window_key)
                current = int(current) if current else 0
                return (current, config.max_requests)
        except Exception as e:
            self._handle_backend_error("获取限流使用情况", e)
            return (0, config.max_requests)
