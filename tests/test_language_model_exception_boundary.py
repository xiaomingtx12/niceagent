"""语言模型领域异常边界回归测试。"""

import pytest

from internal.core.language_model.entities.model_entity import ModelType
from internal.core.language_model.entities.provider_entity import Provider, ProviderEntity


def test_provider_rejects_non_dynamic_model_with_value_error() -> None:
    """Provider 的领域校验不依赖 HTTP 业务异常模块。"""
    provider = Provider(
        name="openai",
        position=1,
        provider_entity=ProviderEntity(),
    )

    with pytest.raises(ValueError, match="仅支持通过模型目录发现并激活的动态模型"):
        provider.get_model_class(ModelType.CHAT)
