"""context_window_calculator 单测。

覆盖：
- model_context_window 已知/未知/带 provider 前缀
- summarization_threshold 默认 0.75 fraction
- estimate_message_tokens 基本行为
- should_trigger_summarization 阈值判断
"""
from langchain_core.messages import HumanMessage, SystemMessage

from internal.core.agent_v2.memory.context_window_calculator import (
    estimate_message_tokens,
    model_context_window,
    should_trigger_summarization,
    summarization_threshold,
)


def test_model_context_window_known_models():
    assert model_context_window("gpt-4o") == 128_000
    assert model_context_window("claude-sonnet-4-5") == 200_000
    assert model_context_window("qwen-plus") == 128_000


def test_model_context_window_with_provider_prefix():
    assert model_context_window("openai/gpt-4o") == 128_000
    assert model_context_window("anthropic/claude-sonnet-4-5") == 200_000


def test_model_context_window_unknown_model_returns_default():
    assert model_context_window("nonexistent-model-xyz") == 128_000
    assert model_context_window("") == 128_000
    assert model_context_window(None) == 128_000


def test_summarization_threshold_default_fraction():
    """0.75 fraction 默认。"""
    assert summarization_threshold(200_000) == 150_000
    assert summarization_threshold(128_000) == 96_000


def test_summarization_threshold_custom_fraction():
    assert summarization_threshold(100_000, fraction=0.5) == 50_000
    assert summarization_threshold(100_000, fraction=0.9) == 90_000


def test_should_trigger_summarization_threshold_check():
    assert should_trigger_summarization(70_000, 100_000) is False
    assert should_trigger_summarization(75_000, 100_000) is True
    assert should_trigger_summarization(75_000, 100_000, fraction=0.9) is False


def test_estimate_message_tokens_basic():
    messages = [SystemMessage(content="you are helpful"), HumanMessage(content="hi")]
    est = estimate_message_tokens(messages)
    assert est.chars > 0
    assert est.approx_tokens > 0
    assert est.approx_tokens == est.chars // 4


def test_estimate_message_tokens_empty_list():
    est = estimate_message_tokens([])
    assert est.chars == 0
    assert est.approx_tokens == 0


def test_estimate_message_tokens_returns_token_count():
    """TokenEstimate.tokens 优先 exact。"""
    from internal.core.agent_v2.memory.context_window_calculator import TokenEstimate
    est = TokenEstimate(chars=100, approx_tokens=25, exact_tokens=30)
    assert est.tokens == 30