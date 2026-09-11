"""一次执行与业务 Run 共用的观测快照约定。"""

from typing import Any, Literal, NotRequired, TypedDict


RunStatus = Literal["running", "completed", "failed", "cancelled", "waiting_input"]
SpanStatus = Literal["running", "ok", "error", "cancelled", "waiting_input", "incomplete"]
SpanKind = Literal["model", "tool", "subagent"]
DETAIL_LIMITS = {"spans": 500, "stages": 100, "events": 200, "executions": 100}


class RunUsage(TypedDict):
    """用量覆盖率独立于 Token 总数，旧快照未知的调用覆盖数保留为空。"""

    trace_id: str
    usage_id: str
    model_count: int
    model_names: list[str]
    model_call_count: int
    tool_call_count: int
    usage_source: str
    usage_present: bool
    usage_call_count: int | None
    missing_usage_call_count: int | None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    input_details: dict[str, int]
    output_details: dict[str, int]
    models: dict[str, dict[str, Any]]


class RunObservability(TypedDict):
    """明细容量按整个 Run 限制，汇总不能从可能截断的列表反算。"""

    schema_version: int
    trace_id: str
    execution_id: str
    execution_count: int
    status: RunStatus
    started_at: str
    finished_at: str | None
    duration_ms: float | None
    attributes: dict[str, Any]
    summary: dict[str, Any]
    usage: RunUsage
    spans: list[dict[str, Any]]
    stages: list[dict[str, Any]]
    events: list[dict[str, Any]]
    executions: list[dict[str, Any]]
    dropped: dict[str, int]
    error_type: NotRequired[str]
