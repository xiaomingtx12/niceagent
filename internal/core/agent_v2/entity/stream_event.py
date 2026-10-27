"""流式事件词表；枚举值保持现有线协议，业务代码使用具名成员。"""

from enum import StrEnum


class EventChannel(StrEnum):
    """Core 与工作台共用通道，表单请求由业务事务产生。"""

    MESSAGES = "messages"
    VALUES = "values"
    TOOLS = "tools"
    LIFECYCLE = "lifecycle"
    CUSTOM = "custom"
    INPUT_REQUESTED = "input.requested"


class MessageEventKind(StrEnum):
    """当前 langchain-protocol 的六种消息事件。"""

    START = "message-start"
    FINISH = "message-finish"
    ERROR = "error"
    BLOCK_START = "content-block-start"
    BLOCK_DELTA = "content-block-delta"
    BLOCK_FINISH = "content-block-finish"


class ContentType(StrEnum):
    """正文、思考和工具参数有不同的展示与合并语义。"""

    TEXT = "text"
    REASONING = "reasoning"
    TOOL_CALL = "tool_call"
    TOOL_CALL_CHUNK = "tool_call_chunk"
    SERVER_TOOL_CALL = "server_tool_call"
    SERVER_TOOL_CALL_CHUNK = "server_tool_call_chunk"


class DeltaKind(StrEnum):
    """当前协议只支持文本、思考、数据追加和结构覆盖四种增量。"""

    TEXT = "text-delta"
    REASONING = "reasoning-delta"
    DATA = "data-delta"
    FIELDS = "block-delta"


class LifecycleEvent(StrEnum):
    """框架结束信号不等同于业务提交完成，根图信号须在投影时过滤。"""

    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    DRAINED = "drained"
    CANCELLED = "cancelled"


class CustomEventType(StrEnum):
    """自定义通道仅公开已登记的进度类型。"""

    CONTEXT_WINDOW = "context_window"
    CONTEXT_COMPACTION = "context_compaction"
    MODEL_PROGRESS = "model_progress"
    MEMORY_FILE_UPDATE = "memory_file_update"


class ModelProgressPhase(StrEnum):
    """模型回调的请求阶段；摘要调用使用独立的开始语义。"""

    STARTED = "started"
    SUMMARIZING = "summarizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


CORE_EVENT_CHANNELS = frozenset(EventChannel) - {EventChannel.INPUT_REQUESTED}
TOOL_ARGUMENT_CONTENT_TYPES = (
    ContentType.TOOL_CALL.value, ContentType.TOOL_CALL_CHUNK.value,
    ContentType.SERVER_TOOL_CALL.value, ContentType.SERVER_TOOL_CALL_CHUNK.value,
)
