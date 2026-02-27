"""
API 限流使用示例

本文件展示了如何在不同场景下使用限流装饰器
"""
from dataclasses import dataclass
from uuid import UUID

from flask_login import login_required, current_user
from injector import inject

from internal.middleware.rate_limit_middleware import rate_limit
from internal.lib.rate_limiter import RateLimitStrategy
from pkg.response import success_json, success_message


@inject
@dataclass
class ExampleHandler:
    """示例控制器 - 展示限流使用方式"""

    # ============================================================
    # 示例 1: 使用预定义配置
    # ============================================================

    @rate_limit(config_key="user")
    @login_required
    def get_user_profile(self):
        """
        获取用户资料
        
        限流配置: 100 请求/分钟 (用户级别)
        """
        return success_json({"user": current_user.name})

    # ============================================================
    # 示例 2: 对话接口限流
    # ============================================================

    @rate_limit(config_key="chat")
    @login_required
    def chat(self):
        """
        对话接口
        
        限流配置: 20 请求/分钟 (对话接口)
        策略: 令牌桶 (允许短时突发)
        """
        # 对话逻辑
        return success_json({"message": "Hello!"})

    # ============================================================
    # 示例 3: 文件上传限流
    # ============================================================

    @rate_limit(config_key="upload")
    @login_required
    def upload_file(self):
        """
        文件上传
        
        限流配置: 10 请求/分钟 (上传接口)
        策略: 固定窗口
        """
        # 上传逻辑
        return success_message("文件上传成功")

    # ============================================================
    # 示例 4: 敏感操作限流
    # ============================================================

    @rate_limit(config_key="sensitive")
    @login_required
    def delete_app(self, app_id: UUID):
        """
        删除应用
        
        限流配置: 5 请求/分钟 (敏感操作)
        策略: 固定窗口
        """
        # 删除逻辑
        return success_message("删除成功")

    # ============================================================
    # 示例 5: 自定义限流配置
    # ============================================================

    @rate_limit(
        max_requests=30,
        window_seconds=60,
        strategy=RateLimitStrategy.SLIDING_WINDOW,
        scope="custom_api"
    )
    @login_required
    def custom_endpoint(self):
        """
        自定义限流配置
        
        限流配置: 30 请求/分钟
        策略: 滑动窗口
        """
        return success_json({"status": "ok"})

    # ============================================================
    # 示例 6: 多层限流保护
    # ============================================================

    @rate_limit(config_key="user")      # 第一层: 用户级别 100/分钟
    @rate_limit(config_key="chat")      # 第二层: 对话接口 20/分钟
    @login_required
    def protected_chat(self):
        """
        多层限流保护的对话接口
        
        限流层级:
        1. 用户级别: 100 请求/分钟
        2. 对话接口: 20 请求/分钟
        
        说明: 两层限流都会检查，任何一层超限都会拒绝请求
        """
        return success_json({"message": "Protected chat"})

    # ============================================================
    # 示例 7: 工作流执行限流
    # ============================================================

    @rate_limit(config_key="workflow")
    @login_required
    def execute_workflow(self, workflow_id: UUID):
        """
        执行工作流
        
        限流配置: 15 请求/分钟 (工作流执行)
        策略: 令牌桶
        """
        # 工作流执行逻辑
        return success_json({"workflow_id": str(workflow_id)})

    # ============================================================
    # 示例 8: 知识库检索限流
    # ============================================================

    @rate_limit(config_key="retrieval")
    @login_required
    def search_knowledge_base(self, dataset_id: UUID):
        """
        知识库检索
        
        限流配置: 30 请求/分钟 (检索接口)
        策略: 滑动窗口
        """
        # 检索逻辑
        return success_json({"results": []})

    # ============================================================
    # 示例 9: 不同用户等级的限流
    # ============================================================

    @login_required
    def premium_feature(self):
        """
        根据用户等级动态限流
        
        - 普通用户: 10 请求/分钟
        - 高级用户: 50 请求/分钟
        - VIP 用户: 200 请求/分钟
        """
        from internal.middleware.rate_limit_middleware import check_rate_limit
        from internal.lib.rate_limiter import RateLimitConfig

        # 根据用户等级选择配置
        if hasattr(current_user, 'is_vip') and current_user.is_vip:
            config = RateLimitConfig(max_requests=200, window_seconds=60)
        elif hasattr(current_user, 'is_premium') and current_user.is_premium:
            config = RateLimitConfig(max_requests=50, window_seconds=60)
        else:
            config = RateLimitConfig(max_requests=10, window_seconds=60)

        # 执行限流检查
        check_rate_limit(config, scope="premium_feature")

        # 业务逻辑
        return success_json({"feature": "premium"})

    # ============================================================
    # 示例 10: 公开 API (基于 IP 限流)
    # ============================================================

    @rate_limit(config_key="ip")
    def public_api(self):
        """
        公开 API (无需登录)
        
        限流配置: 50 请求/分钟 (IP 级别)
        策略: 滑动窗口
        
        说明: 未登录用户基于 IP 地址限流
        """
        return success_json({"public": "data"})

    # ============================================================
    # 示例 11: 批量操作限流
    # ============================================================

    @rate_limit(
        max_requests=5,
        window_seconds=300,  # 5 分钟
        strategy=RateLimitStrategy.FIXED_WINDOW,
        scope="batch_operation"
    )
    @login_required
    def batch_delete(self):
        """
        批量删除操作
        
        限流配置: 5 请求/5分钟
        策略: 固定窗口
        
        说明: 批量操作通常更耗资源，需要更严格的限流
        """
        # 批量删除逻辑
        return success_message("批量删除成功")

    # ============================================================
    # 示例 12: 令牌桶策略 (允许突发流量)
    # ============================================================

    @rate_limit(
        max_requests=100,
        window_seconds=60,
        strategy=RateLimitStrategy.TOKEN_BUCKET,
        scope="burst_allowed"
    )
    @login_required
    def burst_endpoint(self):
        """
        允许突发流量的接口
        
        限流配置: 100 请求/分钟
        策略: 令牌桶
        
        说明: 令牌桶允许短时间内的突发请求，
              但长期平均速率仍受限制
        """
        return success_json({"burst": "allowed"})


# ============================================================
# 使用建议
# ============================================================

"""
1. 选择合适的限流策略:
   - 固定窗口: 简单场景，性能要求高
   - 滑动窗口: 大多数场景，推荐使用
   - 令牌桶: 需要处理突发流量

2. 合理设置限流参数:
   - 根据接口的资源消耗设置限额
   - 考虑用户体验，不要过于严格
   - 监控实际使用情况，动态调整

3. 多层限流保护:
   - 全局限流: 保护整体系统
   - 用户限流: 防止单个用户滥用
   - 接口限流: 保护特定资源

4. 错误处理:
   - 提供清晰的错误提示
   - 告知用户何时可以重试
   - 在前端显示剩余请求数

5. 监控和调优:
   - 记录限流触发日志
   - 分析限流数据
   - 根据实际情况调整配置
"""
