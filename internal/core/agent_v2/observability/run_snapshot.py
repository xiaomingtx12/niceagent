"""在持久化事务内合并执行片段，不依赖框架回调或数据库实现。"""

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from internal.core.agent_v2.entity.run_observability import DETAIL_LIMITS
from internal.core.agent_v2.entity.token_usage import normalize_token_details


def merge_run_observability(
    previous: Mapping[str, Any], current: Mapping[str, Any],
) -> dict[str, Any]:
    """调用方持有 Run 锁并校验执行租约；同一片段重复提交不再次累计。"""
    if not previous:
        return deepcopy(dict(current))
    if previous.get("trace_id") != current.get("trace_id"):
        raise ValueError("不能合并不同 Run 的观测快照")
    if current.get("execution_id") and previous.get("execution_id") == current["execution_id"]:
        return deepcopy(dict(previous))

    result = deepcopy(dict(current))
    result["started_at"] = previous.get("started_at") or current.get("started_at")
    result["duration_ms"] = round(
        float(previous.get("duration_ms") or 0) + float(current.get("duration_ms") or 0), 3,
    )
    result["execution_count"] = int(previous.get("execution_count", 1)) + 1
    result["usage"] = _merge_usage(previous.get("usage", {}), current.get("usage", {}))
    summary = dict(current.get("summary", {}))
    for key in ("model_call_count", "tool_call_count", "subagent_call_count", "retry_count",
                "error_count", "cancelled_call_count", "incomplete_call_count"):
        summary[key] = int(previous.get("summary", {}).get(key, 0)) + int(summary.get(key, 0))
    for key in ("model_count", "usage_present", "usage_call_count", "missing_usage_call_count",
                "input_tokens", "output_tokens", "total_tokens"):
        summary[key] = result["usage"][key]
    result["summary"] = summary

    old_dropped, new_dropped = previous.get("dropped", {}), current.get("dropped", {})
    result["dropped"] = {
        key: int(old_dropped.get(key, 0)) + int(new_dropped.get(key, 0))
        for key in old_dropped.keys() | new_dropped.keys()
    }
    for key, limit in DETAIL_LIMITS.items():
        items = list(previous.get(key, [])) + list(current.get(key, []))
        result[key] = deepcopy(items[:limit])
        result["dropped"][key] = result["dropped"].get(key, 0) + max(0, len(items) - limit)
    return result


def _merge_usage(previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """先标准化每段的别名，再按模型和总量分别相加，兼容旧版用量快照。"""
    tokens = _sum_tokens(previous, current)
    result = {**current, **{key: value for key, value in tokens.items() if key not in {"source", "present"}}}
    for key in ("model_call_count", "tool_call_count"):
        result[key] = int(previous.get(key, 0)) + int(current.get(key, 0))
    for key in ("usage_call_count", "missing_usage_call_count"):
        old, new = previous.get(key), current.get(key)
        result[key] = old + new if old is not None and new is not None else None
    result["usage_present"] = bool(previous.get("usage_present") or current.get("usage_present"))
    old_models, new_models = previous.get("models", {}), current.get("models", {})
    result["models"] = {
        name: _sum_tokens(old_models.get(name, {}), new_models.get(name, {}))
        for name in old_models.keys() | new_models.keys()
    }
    names = set(previous.get("model_names", old_models)) | set(current.get("model_names", new_models))
    result["model_names"] = sorted(names)
    result["model_count"] = len(names)
    return result


def _sum_tokens(previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any]:
    """保留明确的零用量；缺失来源不伪装成某个 Provider 的实测结果。"""
    result: dict[str, Any] = {
        key: int(previous.get(key, 0)) + int(current.get(key, 0))
        for key in ("input_tokens", "output_tokens", "total_tokens")
    }
    for key in ("input_details", "output_details"):
        old = normalize_token_details(previous.get(key))
        new = normalize_token_details(current.get(key))
        result[key] = {name: old.get(name, 0) + new.get(name, 0) for name in old.keys() | new.keys()}
    sources = {item.get("source") for item in (previous, current)} - {None, "unknown"}
    result["source"] = next(iter(sources)) if len(sources) == 1 else "mixed" if sources else "unknown"
    result["present"] = bool(previous.get("present") or current.get("present"))
    return result
