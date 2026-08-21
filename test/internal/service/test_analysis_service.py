"""统计分析服务的 Run 级 Token 指标测试。"""

from datetime import datetime, timedelta
from types import SimpleNamespace

from internal.service.analysis_service import AnalysisService


def _message(**kwargs):
    values = {
        "conversation_id": "conversation",
        "created_by": "account",
        "created_at": datetime(2026, 8, 21),
        "latency": 2.0,
        "total_token_count": 15,
        "total_price": 0.01,
    }
    values.update(kwargs)
    return SimpleNamespace(
        **values,
    )


def test_overview_includes_run_usage_dimensions():
    result = AnalysisService.calculate_overview_indicators_by_messages(
        [
            _message(
                input_token_count=10,
                output_token_count=5,
                cached_token_count=3,
                reasoning_token_count=2,
                model_call_count=2,
                tool_call_count=1,
            )
        ]
    )

    assert result["total_token_count"] == 15
    assert result["input_token_count"] == 10
    assert result["output_token_count"] == 5
    assert result["cached_token_count"] == 3
    assert result["reasoning_token_count"] == 2
    assert result["model_call_count"] == 2
    assert result["tool_call_count"] == 1


def test_trend_includes_run_usage_dimensions():
    end_at = datetime(2026, 8, 22)
    result = AnalysisService.calculate_trend_by_messages(
        end_at,
        1,
        [
            _message(
                created_at=end_at - timedelta(hours=1),
                input_token_count=10,
                output_token_count=5,
                model_call_count=2,
                tool_call_count=1,
            )
        ],
    )

    assert result["total_token_count_trend"]["y_axis"] == [15]
    assert result["input_token_count_trend"]["y_axis"] == [10]
    assert result["output_token_count_trend"]["y_axis"] == [5]
    assert result["model_call_count_trend"]["y_axis"] == [2]
    assert result["tool_call_count_trend"]["y_axis"] == [1]


def test_model_usage_aggregates_persisted_breakdown():
    result = AnalysisService.calculate_model_usage_by_messages(
        [
            _message(
                token_usage={
                    "model-a": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "total_tokens": 15,
                        "input_details": {"cached_tokens": 3},
                        "output_details": {"reasoning_tokens": 2},
                    }
                }
            )
        ]
    )

    assert result["model-a"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "cached_tokens": 3,
        "reasoning_tokens": 2,
    }
