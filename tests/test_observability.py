"""离线验证调用汇总、重试口径及统计页计算，不连接模型或数据库。"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from internal.core.agent_v2.observability.run_observer import PlatformAgentRunObserver
from internal.service.analysis_service import AnalysisService


def model_result() -> LLMResult:
    """输入显著大于输出，以检测用总 Token 冒充输出吞吐的回归。"""
    return LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(
                        content="PRIVATE_RESPONSE",
                        usage_metadata={
                            "input_tokens": 900,
                            "output_tokens": 100,
                            "total_tokens": 1000,
                        },
                        response_metadata={"model_name": "model-a"},
                    )
                )
            ]
        ]
    )


def test_same_name_after_failure_is_not_a_retry():
    """同名工具可能是另一个任务，只有实际重试回调才能增加重试次数。"""
    observer = PlatformAgentRunObserver(uuid4())
    parent, first, second = uuid4(), uuid4(), uuid4()
    observer.on_tool_start(
        {"name": "search"}, "PRIVATE_QUERY", run_id=first, parent_run_id=parent
    )
    observer.on_tool_error(RuntimeError("PRIVATE_ERROR"), run_id=first)
    observer.on_tool_start(
        {"name": "search"}, "another query", run_id=second, parent_run_id=parent
    )
    assert observer.snapshot()["summary"]["retry_count"] == 0
    observer.on_retry(None, run_id=second)
    observer.on_tool_end("result", run_id=second)
    snapshot = observer.snapshot()
    assert snapshot["summary"]["retry_count"] == 1
    assert snapshot["summary"]["tool_call_count"] == 2
    assert snapshot["summary"]["error_count"] == 1
    assert snapshot["spans"][1]["retry_number"] == 1
    assert "PRIVATE" not in str(snapshot)


def test_parallel_completion_counts_each_call_once_even_when_spans_are_truncated():
    """只截断排障明细，汇总必须覆盖全部调用且不重复累计结束回调。"""
    observer = PlatformAgentRunObserver(uuid4(), max_spans=1)
    calls = [uuid4() for _ in range(12)]
    for call in calls:
        observer.on_chat_model_start({"name": "model-a"}, [], run_id=call)

    def finish(call):
        """模拟并发子图，以及相同调用的重复结束通知。"""
        observer.on_llm_end(model_result(), run_id=call)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(finish, calls * 2))
    observer.finish("completed")
    snapshot = observer.snapshot()
    assert len(snapshot["spans"]) == 1
    assert snapshot["dropped"]["spans"] == 11
    assert snapshot["summary"]["model_call_count"] == 12
    assert snapshot["usage"]["total_tokens"] == 12000
    assert snapshot["usage"]["models"]["model-a"]["output_tokens"] == 1200
    snapshot["usage"]["models"]["model-a"]["output_tokens"] = 0
    assert observer.build_run_usage()["output_tokens"] == 1200


def test_subagent_parent_and_tool_error_remain_visible():
    """内部 Chain 不显示为 Span，但不能丢失模型与 SubAgent 的父子关联。"""
    observer = PlatformAgentRunObserver(uuid4())
    task, chain, model = uuid4(), uuid4(), uuid4()
    observer.on_tool_start(
        {"name": "task"}, "private", run_id=task, inputs={"subagent_type": "research"}
    )
    observer.on_chain_start({}, {}, run_id=chain, parent_run_id=task)
    observer.on_chat_model_start({}, [], run_id=model, parent_run_id=chain)
    observer.on_llm_end(model_result(), run_id=model)
    observer.on_chain_end({}, run_id=chain)
    observer.on_tool_end(
        ToolMessage(content="private", tool_call_id=str(task), status="error"),
        run_id=task,
    )
    observer.finish("completed")
    snapshot = observer.snapshot()
    assert snapshot["spans"][1]["parent_span_id"] == str(task)
    assert snapshot["spans"][0]["kind"] == "subagent"
    assert snapshot["summary"]["subagent_call_count"] == 1
    assert snapshot["summary"]["error_count"] == 1


def message(created_at, **overrides):
    """保留真实字段形状，同时覆盖历史消息缺少用量明细的情况。"""
    return SimpleNamespace(
        **{
            "created_at": created_at,
            "created_by": "user-a",
            "conversation_id": "conversation-a",
            "input_token_count": 900,
            "output_token_count": 100,
            "total_token_count": 1000,
            "latency": 10.0,
            "total_price": 0.1,
            **overrides,
        }
    )


def test_overview_uses_output_tokens_and_per_message_latency():
    """两条回复属于一个会话；吞吐按总输出除总耗时，不能平均单次速率。"""
    now = datetime(2026, 9, 8)
    result = AnalysisService.calculate_overview_indicators_by_messages(
        [
            message(now),
            message(now, latency=30.0, output_token_count=300, total_token_count=1200),
        ]
    )
    assert result["total_messages"] == 2
    assert result["active_accounts"] == 1
    assert result["avg_of_conversation_messages"] == 2
    assert result["average_latency"] == 20
    assert result["token_output_rate"] == 10
    assert result["total_token_count"] == 2200
    assert result["cost_consumption"] == pytest.approx(0.2)
    assert result["cached_token_count"] == 0


def test_daily_trends_keep_empty_days_and_exclude_interval_end():
    """固定三日窗口，跨日数据与午夜边界都使用左闭右开区间。"""
    start = datetime(2026, 9, 6)
    result = AnalysisService.calculate_trend_by_messages(
        start + timedelta(days=3),
        3,
        [
            message(start - timedelta(seconds=1)),
            message(start),
            message(start + timedelta(hours=2), latency=30),
            message(start + timedelta(days=2)),
            message(start + timedelta(days=3)),
        ],
    )
    assert result["total_messages_trend"]["y_axis"] == [2, 0, 1]
    assert result["average_latency_trend"]["y_axis"] == [20, 0, 10]
    assert result["total_token_count_trend"]["y_axis"] == [2000, 0, 1000]
    assert result["avg_of_conversation_messages_trend"]["y_axis"] == [2, 0, 1]
    assert result["active_accounts_trend"]["x_axis"] == [
        int((start + timedelta(days=day)).timestamp()) for day in range(3)
    ]


def test_empty_period_and_zero_latency_do_not_divide_by_zero():
    """无数据与历史零耗时不应导致统计页失败。"""
    empty = AnalysisService.calculate_overview_indicators_by_messages([])
    assert all(value == 0 for value in empty.values())
    current = AnalysisService.calculate_overview_indicators_by_messages(
        [message(datetime(2026, 9, 8), latency=0)]
    )
    assert current["token_output_rate"] == 0
    assert all(
        value == 0
        for value in AnalysisService.calculate_pop_by_overview_indicators(
            current, empty
        ).values()
    )
