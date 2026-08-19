import pytest

from internal.exception import ValidateErrorException
from internal.service.app_config_service import AppConfigService


def test_long_term_memory_normalization_always_enables_memory():
    assert AppConfigService._normalize_long_term_memory({"enable": False}) == {
        "enable": True
    }
    assert AppConfigService._normalize_long_term_memory({}) == {"enable": True}
    assert AppConfigService._normalize_long_term_memory(None) == {"enable": True}


def test_long_term_memory_normalization_rejects_invalid_shape():
    with pytest.raises(ValidateErrorException, match="长期记忆配置格式错误"):
        AppConfigService._normalize_long_term_memory({"enable": "false"})
