"""Agent Protocol 工作台的输入协议 Schema。

协议字段由前端 SDK 发送，HTTP 层只负责读取 JSON；业务字段的结构和
限制在这里集中定义，避免 Service 里散落大量 ``Mapping`` 取值代码。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    AliasChoices,
    RootModel,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
)

from internal.entity import WorkbenchRunInput
from internal.exception import ValidateErrorException


class WorkbenchCommandMessageReq(BaseModel):
    """一次 run.start 中最后一条用户消息。"""

    model_config = ConfigDict(extra="allow")

    id: StrictStr | None = Field(default=None, max_length=255)
    content: Any = ""

    @property
    def text(self) -> str:
        """将协议消息内容转换为纯文本查询。"""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, list):
            return "".join(
                str(part.get("text", ""))
                for part in self.content
                if isinstance(part, Mapping) and part.get("type") in {"text", None}
            )
        return str(self.content or "")


class WorkbenchCommandInputReq(BaseModel):
    """从 Agent Protocol command 中提取出的业务输入。"""

    model_config = ConfigDict(extra="allow")

    messages: list[WorkbenchCommandMessageReq] = Field(default_factory=list)
    query: StrictStr | None = None
    image_urls: list[StrictStr] = Field(default_factory=list, max_length=10)
    file_ids: list[StrictStr] = Field(default_factory=list, max_length=5)
    selected_skills: list[Annotated[StrictStr, Field(min_length=1, max_length=64)]] = (
        Field(default_factory=list, max_length=20)
    )

    @field_validator("image_urls", "file_ids", "selected_skills", mode="before")
    def normalize_list(value: Any) -> list[Any]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("必须是数组")
        return value

    @field_validator("selected_skills")
    @classmethod
    def validate_selected_skills(cls, value: list[str]) -> list[str]:
        """规范化并拒绝重复的本轮 Skill 选择。"""
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("所选 Skill 名称不能为空")
        if len(normalized) != len(set(normalized)):
            raise ValueError("所选 Skill 不能重复")
        return normalized

    def to_run_input(self) -> WorkbenchRunInput:
        """把已校验的协议输入转换成核心服务使用的运行输入。"""
        message = self.messages[-1] if self.messages else None
        query = message.text if message is not None else (self.query or "")
        if not query:
            raise ValidateErrorException("run.start 需要非空的用户输入")
        return WorkbenchRunInput(
            query=query,
            image_urls=tuple(self.image_urls),
            file_ids=tuple(self.file_ids),
            selected_skills=tuple(self.selected_skills),
            input_message_id=message.id if message else None,
        )


class WorkbenchCommandReq(BaseModel):
    """工作台当前支持的 Agent Protocol 命令。"""

    model_config = ConfigDict(extra="allow")

    type: StrictStr | None = None
    id: StrictInt | None = None
    method: Literal["run.start"]
    params: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_mapping(cls, command: Any) -> "WorkbenchCommandReq":
        if not isinstance(command, Mapping):
            raise ValidateErrorException("工作台命令必须是 JSON 对象")
        try:
            return cls.model_validate(dict(command))
        except ValidationError as exc:
            raise ValidateErrorException(f"工作台命令格式错误: {exc}") from exc

    def input_data(self) -> WorkbenchCommandInputReq:
        raw = self.params.get("input", self.params)
        if not isinstance(raw, Mapping):
            raise ValidateErrorException("run.start 的 input 必须是对象")
        try:
            value = WorkbenchCommandInputReq.model_validate(dict(raw))
        except ValidationError as exc:
            raise ValidateErrorException(f"工作台输入格式错误: {exc}") from exc
        if not value.messages and not value.query:
            raise ValidateErrorException("run.start 需要 input.messages 或 query")
        return value

    @property
    def command_id(self) -> int | None:
        """返回协议命令 id。"""
        return self.id if isinstance(self.id, int) else None

    def to_run_input(self) -> WorkbenchRunInput:
        """转换为 Service 可直接使用的可信运行输入。"""
        return self.input_data().to_run_input()


class WorkbenchEventParams(BaseModel):
    """一条工作台协议事件的公共参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    namespace: tuple[StrictStr, ...] = Field(default_factory=tuple)
    timestamp: Any = None
    data: Any = None
    metadata: dict[StrictStr, Any] = Field(default_factory=dict)
    node: StrictStr | None = None


class _WorkbenchToolEventBase(BaseModel):
    """工具事件的公共字段和 LangChain 兼容解析。"""

    model_config = ConfigDict(extra="ignore")

    tool_call_id: StrictStr = Field(
        validation_alias=AliasChoices("tool_call_id", "id")
    )

    @field_validator("tool_call_id", mode="before")
    @classmethod
    def normalize_tool_call_id(cls, value: Any) -> str:
        """把旧事件中的调用标识统一为协议字段。"""
        return str(value)


class WorkbenchToolStartedData(_WorkbenchToolEventBase):
    """工具开始事件。"""

    event: Literal["tool-started"]
    tool_name: StrictStr = Field(
        validation_alias=AliasChoices("tool_name", "name")
    )
    input: Any = None


class WorkbenchToolOutputDeltaData(_WorkbenchToolEventBase):
    """工具输出增量事件。"""

    event: Literal["tool-output-delta"]
    delta: Any = None


class WorkbenchToolFinishedData(_WorkbenchToolEventBase):
    """工具完成事件。"""

    event: Literal["tool-finished"]
    output: Any = None


class WorkbenchToolErrorData(_WorkbenchToolEventBase):
    """工具失败事件。"""

    event: Literal["tool-error"]
    message: StrictStr

    @field_validator("message", mode="before")
    @classmethod
    def normalize_message(cls, value: Any) -> str:
        """将异常对象转换为协议字符串，不改写或截断错误正文。"""
        return "" if value is None else str(value)


class WorkbenchToolEventData(
    RootModel[
        Annotated[
            WorkbenchToolStartedData
            | WorkbenchToolOutputDeltaData
            | WorkbenchToolFinishedData
            | WorkbenchToolErrorData,
            Field(discriminator="event"),
        ]
    ]
):
    """标准工具生命周期事件的带判别联合。"""


class WorkbenchEvent(BaseModel):
    """经过公开通道投影、可持久化和对外传输的工作台事件。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["event"]
    event_id: StrictStr = Field(min_length=1)
    seq: StrictInt = Field(ge=0)
    method: Literal["messages", "values", "tools", "lifecycle"]
    params: WorkbenchEventParams


@dataclass(frozen=True)
class WorkbenchLocalEventBatchResp:
    """一次本地工作台事件队列读取结果。"""

    events: tuple[WorkbenchEvent, ...]
    oldest_event_sequence: int | None
    timed_out: bool
    finished: bool


class WorkbenchSubscriptionReq(BaseModel):
    """SSE 订阅过滤条件。

    ``input`` 和 ``checkpoints`` 是 LangGraph SDK 根订阅的控制 channel。
    工作台当前不发布这两类事件，但必须接受它们，才能兼容官方
    ``useStream`` 的默认订阅集合。
    """

    model_config = ConfigDict(extra="ignore")

    since: StrictInt = Field(default=0, ge=0)
    channels: list[
        Literal[
            "messages",
            "values",
            "tools",
            "lifecycle",
            "input",
            "checkpoints",
        ]
    ] = Field(default_factory=list)
    namespaces: list[list[StrictStr]] = Field(default_factory=list)
    depth: StrictInt | None = Field(default=None, ge=0)

    @classmethod
    def from_mapping(
        cls,
        body: Mapping[str, Any] | None,
        *,
        last_event_id: str | None = None,
    ) -> "WorkbenchSubscriptionReq":
        payload = dict(body) if isinstance(body, Mapping) else {}
        if "since" not in payload and last_event_id:
            sequence = _parse_last_event_sequence(last_event_id)
            if sequence is not None:
                payload["since"] = sequence
        try:
            return cls.model_validate(payload)
        except ValidationError as exc:
            raise ValidateErrorException(f"工作台订阅参数错误: {exc}") from exc

    def matches(self, event: WorkbenchEvent) -> bool:
        """判断协议事件是否符合订阅过滤条件。"""
        if self.channels and event.method not in self.channels:
            return False
        namespace = event.params.namespace
        if self.namespaces and not any(
            namespace[: len(prefix)] == tuple(prefix) for prefix in self.namespaces
        ):
            return False
        return not (self.depth is not None and len(namespace) > self.depth)


def _parse_last_event_sequence(value: str) -> int | None:
    """解析 SSE 游标；cursor-reset 的事件 ID 使用重放起点而非事件序号。"""
    parts = value.strip().split(":")
    try:
        if len(parts) >= 2 and parts[-2] == "cursor-reset":
            return max(0, int(parts[-1]) - 1)
        return max(0, int(parts[-1]))
    except (ValueError, TypeError):
        return None


class WorkbenchCommandResultResp(BaseModel):
    """命令成功响应中的运行标识。"""

    run_id: UUID


class WorkbenchCommandSuccessResp(BaseModel):
    """Agent Protocol 命令成功响应。"""

    type: Literal["success"] = "success"
    id: int = 0
    result: WorkbenchCommandResultResp
    meta: dict[str, int] | None = None

    @classmethod
    def create(
        cls,
        *,
        command_id: int | None,
        run_id: UUID,
        applied_through_sequence: int | None = None,
    ) -> "WorkbenchCommandSuccessResp":
        """创建命令成功响应。"""
        meta = (
            {"applied_through_seq": applied_through_sequence}
            if applied_through_sequence is not None
            else None
        )
        return cls(
            id=command_id or 0,
            result=WorkbenchCommandResultResp(run_id=run_id),
            meta=meta,
        )


class WorkbenchCommandErrorResp(BaseModel):
    """Agent Protocol 命令错误响应。"""

    type: Literal["error"] = "error"
    id: int | None = None
    error: Literal["invalid_argument"] = "invalid_argument"
    message: str


class WorkbenchReplayBatchResp(BaseModel):
    """Repository 返回的一批短期重放事件。"""

    events: list[WorkbenchEvent] = Field(default_factory=list)
    oldest_event_sequence: int | None = Field(default=None, ge=1)


class WorkbenchHistoryMessageResp(BaseModel):
    """状态水合和消息分页中的一条对话消息。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: StrictStr
    type: Literal["human", "ai"]
    content: Any
    additional_kwargs: dict[str, Any] = Field(default_factory=dict)


class WorkbenchMessagePageInfoResp(BaseModel):
    """工作台历史消息分页信息。"""

    has_more: bool = False
    next_before: UUID | None = None


class WorkbenchMessagePageResp(BaseModel):
    """工作台历史消息分页响应。"""

    messages: list[WorkbenchHistoryMessageResp] = Field(default_factory=list)
    page: WorkbenchMessagePageInfoResp


class WorkbenchRunMetadataResp(BaseModel):
    """工作台状态中的当前 Run 摘要。"""

    run_id: UUID | None = None
    status: str = "idle"
    last_event_sequence: int = Field(default=0, ge=0)


class WorkbenchStateResp(BaseModel):
    """兼容 LangGraph SDK 的工作台状态响应。"""

    values: dict[str, Any] = Field(default_factory=dict)
    next: list[str] = Field(default_factory=list)
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    metadata: WorkbenchRunMetadataResp


class WorkbenchCancelResp(BaseModel):
    """工作台取消指定 Run 的响应。"""

    cancel_requested: bool
    run_id: UUID | None = None
    status: str


class WorkbenchRunObservabilityResp(BaseModel):
    """一个平台 Agent Run 的可查询观测结果。"""

    run_id: UUID
    trace_id: UUID
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    observability: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "WorkbenchCommandReq",
    "WorkbenchCommandInputReq",
    "WorkbenchCommandMessageReq",
    "WorkbenchSubscriptionReq",
    "WorkbenchCommandResultResp",
    "WorkbenchCommandSuccessResp",
    "WorkbenchCommandErrorResp",
    "WorkbenchEvent",
    "WorkbenchEventParams",
    "WorkbenchLocalEventBatchResp",
    "WorkbenchToolEventData",
    "WorkbenchToolErrorData",
    "WorkbenchToolFinishedData",
    "WorkbenchToolOutputDeltaData",
    "WorkbenchToolStartedData",
    "WorkbenchHistoryMessageResp",
    "WorkbenchReplayBatchResp",
    "WorkbenchMessagePageInfoResp",
    "WorkbenchMessagePageResp",
    "WorkbenchRunMetadataResp",
    "WorkbenchStateResp",
    "WorkbenchCancelResp",
    "WorkbenchRunObservabilityResp",
]
