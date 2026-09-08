"""连接池生命周期错误与真正瞬时断连使用不同的重试策略。"""

from unittest.mock import Mock

import pytest
from psycopg import InterfaceError, OperationalError
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests

from internal.core.agent_v2.persistence.postgres_connection import execute_with_postgres_retry


@pytest.mark.parametrize("error_type", [PoolClosed, PoolTimeout, TooManyRequests])
def test_pool_errors_are_not_retried(error_type):
    """不重复等待一个已关闭或已耗尽的池，也不误报新连接重试。"""
    error = error_type("pool unavailable")
    operation = Mock(side_effect=error)
    logger = Mock()
    with pytest.raises(error_type) as caught:
        execute_with_postgres_retry(operation, logger=logger, resource="test", operation_name="read")
    assert caught.value is error
    operation.assert_called_once()
    logger.warning.assert_not_called()


@pytest.mark.parametrize("error_type", [OperationalError, InterfaceError])
def test_transient_disconnect_is_retried_once(error_type):
    """保持实际断连后的单次幂等恢复能力。"""
    operation = Mock(side_effect=[error_type("connection lost"), "ok"])
    result = execute_with_postgres_retry(operation, logger=Mock(), resource="test", operation_name="read")
    assert result == "ok"
    assert operation.call_count == 2


def test_repeated_disconnect_is_propagated_after_one_retry():
    """持续断连必须有界失败，不能不断重试拖住运行线程。"""
    operation = Mock(side_effect=OperationalError("connection lost"))
    with pytest.raises(OperationalError):
        execute_with_postgres_retry(operation, logger=Mock(), resource="test", operation_name="read")
    assert operation.call_count == 2
