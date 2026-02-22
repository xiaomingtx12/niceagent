"""API Key 辅助函数，用于获取用户配置的 API Key"""

from uuid import UUID

from internal.exception import ValidateErrorException


def _get_provider_api_key_service():
    """从 Flask 应用上下文获取 ProviderApiKeyService"""
    try:
        from flask import current_app
        from app.http.module import injector
        from internal.service.provider_api_key_service import ProviderApiKeyService

        return injector.get(ProviderApiKeyService)
    except Exception as e:
        raise ValidateErrorException(f"无法获取 API Key 服务: {str(e)}")


def get_api_key_for_provider(
    provider_name: str,
    account_id: UUID,
) -> str:
    """
    获取指定提供商的 API Key（仅从用户配置获取）

    Args:
        provider_name: 提供商名称，如 'tongyi', 'openai', 'moonshot' 等
        account_id: 用户账号 ID（必填）

    Returns:
        API Key 字符串

    Raises:
        ValidateErrorException: 如果用户未配置该提供商的 API Key
    """
    if not account_id:
        raise ValidateErrorException(
            f"无法获取 {provider_name} 的 API Key，用户信息缺失"
        )

    provider_api_key_service = _get_provider_api_key_service()

    try:
        user_api_key = provider_api_key_service.get_active_api_key_by_provider(
            account_id, provider_name
        )
        if user_api_key:
            return user_api_key
    except Exception as e:
        raise ValidateErrorException(f"获取 {provider_name} 的 API Key 失败: {str(e)}")

    # 如果用户未配置，抛出异常
    raise ValidateErrorException(
        f"您还未配置 {provider_name} 的 API 密钥，请先在设置中添加"
    )


def get_dashscope_api_key(account_id: UUID) -> str:
    """
    获取 DashScope API Key 的便捷方法

    Args:
        account_id: 用户账号 ID（必填）

    Returns:
        API Key 字符串

    Raises:
        ValidateErrorException: 如果用户未配置 API Key
    """
    return get_api_key_for_provider("tongyi", account_id)
