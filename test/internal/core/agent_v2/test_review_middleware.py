"""apply_keyword_filter 纯函数单测。

覆盖：
- 关键词命中（中文/英文/大小写）
- 单词边界（避免误命中 'app' → 'p'）
- 空输入 / 空关键词列表
- 关键词列表含空字符串
"""
from internal.core.agent_v2.middleware.review_middleware import apply_keyword_filter


def test_apply_keyword_filter_hits_chinese_keyword():
    assert apply_keyword_filter("hello 笨蛋 world", ["笨蛋"]) == "笨蛋"


def test_apply_keyword_filter_is_case_insensitive_by_default():
    assert apply_keyword_filter("Hello BAD-Word", ["bad-word"]) == "bad-word"


def test_apply_keyword_filter_returns_none_when_no_match():
    assert apply_keyword_filter("safe text", ["forbidden"]) is None


def test_apply_keyword_filter_respects_word_boundary():
    """'app' 不应被 'p' 命中（单词边界）。"""
    assert apply_keyword_filter("application", ["p"]) is None
    assert apply_keyword_filter("a p", ["p"]) == "p"


def test_apply_keyword_filter_handles_empty_inputs():
    assert apply_keyword_filter("", ["x"]) is None
    assert apply_keyword_filter("text", []) is None
    assert apply_keyword_filter("text", [""]) is None


def test_apply_keyword_filter_skips_empty_keywords_in_list():
    assert apply_keyword_filter("hello world", ["", "world"]) == "world"