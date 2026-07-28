"""业务 SummarizationMiddleware 单测。

覆盖:
- build_summarization_trigger: 边界 (None / 1 / 2 / 20 / 100)
- build_summarization_keep: 反算 (保证 keep < trigger)
- build_summarization_middleware: max_message_count<=0 返回 None;
  >0 返回 MyDifySummarizationMiddleware 实例
- 透传 backend 给 deepagents 实例
- 关键: .name = "SummarizationMiddleware"，由 DeepAgents 原位替换默认实例

约束 (硬约束 #5): 不重写 _should_summarize; 只测业务包装层.
不依赖 DB / LLM / deepagents graph 启动.
"""
import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from internal.core.agent_v2.middleware.summarization_middleware import (
    MyDifySummarizationMiddleware,
    build_summarization_keep,
    build_summarization_middleware,
    build_summarization_trigger,
)


def _llm():
    return FakeListChatModel(responses=["ok"])


def _backend():
    """用 StateBackend 占位 (DeepAgentsSummarizationMiddleware.backend 必填)."""
    from deepagents.backends import StateBackend
    return StateBackend()


# ---------- build_summarization_trigger ----------


def test_trigger_zero_returns_none():
    assert build_summarization_trigger(0) is None


def test_trigger_negative_returns_none():
    assert build_summarization_trigger(-5) is None


def test_trigger_one_returns_messages_1():
    assert build_summarization_trigger(1) == ("messages", 1)


def test_trigger_default_20():
    """AgentConfig.max_message_count 默认 20. trigger 透传."""
    assert build_summarization_trigger(20) == ("messages", 20)


def test_trigger_large_value_passes_through():
    assert build_summarization_trigger(100) == ("messages", 100)


# ---------- build_summarization_keep ----------


def test_keep_one_when_trigger_le_one():
    assert build_summarization_keep(1) == ("messages", 1)
    assert build_summarization_keep(0) == ("messages", 1)
    assert build_summarization_keep(-1) == ("messages", 1)


def test_keep_is_strictly_less_than_trigger_for_normal_values():
    """keep 必须 < trigger 避免 deadlock (永远触发)."""
    for n in [4, 10, 20, 50, 100]:
        trigger = build_summarization_trigger(n)
        keep = build_summarization_keep(n)
        assert trigger == ("messages", n)
        assert keep == ("messages", n // 2)
        assert keep[1] < trigger[1]


def test_keep_for_two_returns_one():
    """max_message_count=2 → keep=1 (避免 keep == trigger)."""
    assert build_summarization_keep(2) == ("messages", 1)


# ---------- build_summarization_middleware ----------


def test_build_returns_none_when_disabled():
    """max_message_count<=0 → 关闭压缩, 不挂中间件."""
    assert build_summarization_middleware(llm=_llm(), max_message_count=0, backend=_backend()) is None
    assert build_summarization_middleware(llm=_llm(), max_message_count=-1, backend=_backend()) is None


def test_build_returns_deepagents_summarization_middleware_instance():
    """max_message_count>0 → 业务 MyDifySummarizationMiddleware 实例 (deepagents 子类)."""
    from deepagents.middleware.summarization import _DeepAgentsSummarizationMiddleware

    mw = build_summarization_middleware(
        llm=_llm(), max_message_count=20, backend=_backend()
    )
    assert isinstance(mw, MyDifySummarizationMiddleware)
    # 业务包装层是 _DeepAgentsSummarizationMiddleware 的子类 (复用 deepagents 逻辑)
    assert isinstance(mw, _DeepAgentsSummarizationMiddleware)


def test_build_uses_provided_llm():
    """业务包装层透传 llm 给 deepagents 中间件 (同一 LLM 生成 summary)."""
    mw = build_summarization_middleware(
        llm=_llm(), max_message_count=20, backend=_backend()
    )
    # deepagents SummarizationMiddleware.model 即构造传入的 llm
    assert mw.model is not None
    assert isinstance(mw.model, type(_llm()))


def test_build_passes_backend_for_offload():
    """透传 backend 到 deepagents 实例 (offload 超长 tool_call 内容)."""
    backend = _backend()
    mw = build_summarization_middleware(
        llm=_llm(),
        max_message_count=10,
        backend=backend,
    )
    assert isinstance(mw, MyDifySummarizationMiddleware)
    # deepagents SummarizationMiddleware 内部存 backend 为 _backend (private)
    assert mw._backend is backend


def test_name_replaces_deepagents_default_middleware():
    """同名使 DeepAgents 在 core stack 原位置替换默认摘要器。"""
    mw = build_summarization_middleware(
        llm=_llm(), max_message_count=20, backend=_backend()
    )
    assert mw.name == "SummarizationMiddleware"


def test_name_matches_deepagents_default_for_in_place_replacement():
    """DeepAgents 0.6.12 的 custom middleware 合并契约是按名称替换。"""
    from deepagents.middleware.summarization import _DeepAgentsSummarizationMiddleware

    mw_a = MyDifySummarizationMiddleware(model=_llm(), backend=_backend())
    mw_b = _DeepAgentsSummarizationMiddleware(model=_llm(), backend=_backend())
    assert mw_b.name == "SummarizationMiddleware"
    assert mw_a.name == mw_b.name


def test_passes_through_trigger_and_keep():
    """build_..._middleware 内部用 build_summarization_trigger/keep; trigger/keep 透传.

    deepagents `_DeepAgentsSummarizationMiddleware` 把 trigger/keep 存在 self._lc_helper
    (langchain SummarizationMiddleware 实例) 上 —— 业务上我们通过构造参数传,
    它会传给底层 langchain helper. 这里读 `_lc_helper.trigger` / `.keep` 验证透传.
    """
    mw = build_summarization_middleware(
        llm=_llm(), max_message_count=20, backend=_backend()
    )
    # _DeepAgentsSummarizationMiddleware._lc_helper 即 langchain 真实 trigger/keep 持有者
    assert mw._lc_helper.trigger == ("messages", 20)
    assert mw._lc_helper.keep == ("messages", 10)


def test_passes_through_trigger_for_max_count_two():
    """max_message_count=2 → keep=1 (避免 keep == trigger 死循环)."""
    mw = build_summarization_middleware(
        llm=_llm(), max_message_count=2, backend=_backend()
    )
    assert mw._lc_helper.trigger == ("messages", 2)
    assert mw._lc_helper.keep == ("messages", 1)
