"""Agent Protocol 工作台的输入协议 Schema。

协议字段由前端 SDK 发送，HTTP 层只负责读取 JSON；业务字段的结构和
限制在这里集中定义，避免 Service 里散落大量 ``Mapping`` 取值代码。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from internal.entity import AgentRunInput
from internal.entity.agent_run_entity import AgentModelSelection
from internal.exception import ValidateErrorException

# 公开协议方法词表：DTO 校验、历史回放过滤和持久化查询共用同一来源，新增通道只改这里。
PUBLIC_EVENT_METHODS = (
    "messages", "values", "tools", "lifecycle", "custom", "input.requested",
)


class WorkbenchCommandMessageReq(BaseModel):
    """一次 run.start 中最后一条用户消息。"""

    model_config = ConfigDict(extra="allow")

    id: StrictStr | None = Field(default=None, max_length=255)
    type: Literal["human", "user"] | None = None
    role: Literal["human", "user"] | None = None
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
    planning_enabled: bool = Field(default=False, strict=True)
    reasoning_effort: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    model_selection: AgentModelSelection | None = None
    image_urls: list[StrictStr] = Field(default_factory=list, max_length=10)
    file_ids: list[UUID] = Field(default_factory=list, max_length=5)
    selected_skills: list[Annotated[StrictStr, Field(min_length=1, max_length=64)]] = (
        Field(default_factory=list, max_length=20)
    )

    @field_validator("image_urls", "file_ids", "selected_skills", mode="before")
    def normalize_list(value: Any) -> list[Any]:
        """兼容旧客户端的 null，其他非数组输入仍拒绝。"""
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

    @field_validator("file_ids")
    @classmethod
    def reject_duplicate_file_ids(cls, value: list[UUID]) -> list[UUID]:
        """同一附件在一次调用中只能出现一次。"""
        if len(value) != len(set(value)):
            raise ValueError("附件不能重复提交")
        return value

    def to_run_input(self) -> AgentRunInput:
        """把已校验的协议输入转换成核心服务使用的运行输入。"""
        message = self.messages[-1] if self.messages else None
        query = message.text if message is not None else (self.query or "")
        query = query.strip()
        if not query:
            raise ValidateErrorException("run.start 需要非空的用户输入")
        return AgentRunInput(
            query=query,
            planning_enabled=self.planning_enabled,
            image_urls=tuple(self.image_urls),
            file_ids=tuple(self.file_ids),
            selected_skills=tuple(self.selected_skills),
            input_message_id=message.id if message else None,
            reasoning_effort=self.reasoning_effort,
            model_selection=self.model_selection,
        )


class WorkbenchInputResponseReq(BaseModel):
    """只接受已知中断的答案，不开放图跳转或状态任意写入。"""

    model_config = ConfigDict(extra="forbid")

    namespace: list[StrictStr] = Field(default_factory=list, max_length=0)
    interrupt_id: StrictStr = Field(min_length=1, max_length=255)
    response: dict[str, Any]


class WorkbenchInputRespondReq(BaseModel):
    """兼容 SDK 单个与批量 input.respond 请求。"""

    model_config = ConfigDict(extra="forbid")

    responses: list[WorkbenchInputResponseReq] = Field(min_length=1, max_length=40)

    @model_validator(mode="before")
    @classmethod
    def normalize_single(cls, value: Any) -> Any:
        """将官方单个中断响应转换为统一的批量表示。"""
        if isinstance(value, Mapping) and "interrupt_id" in value and "responses" not in value:
            return {"responses": [dict(value)]}
        return value

    @model_validator(mode="after")
    def reject_duplicates(self) -> "WorkbenchInputRespondReq":
        """一条命令中的中断只能出现一次。"""
        ids = [item.interrupt_id for item in self.responses]
        if len(ids) != len(set(ids)):
            raise ValueError("中断 ID 不能重复")
        return self


class WorkbenchInputReopenReq(BaseModel):
    """用取消事件序号限定恢复批次，防止迟到请求复活后来停止的任务。"""

    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    cancellation_sequence: int = Field(gt=0, strict=True)


class WorkbenchCommandReq(BaseModel):
    """在请求解析时确定命令参数类型，分派阶段只消费已规范化的数据。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    type: StrictStr | None = None
    id: StrictInt | None = None
    method: Literal["run.start", "input.respond", "input.reopen"]
    params: AgentRunInput | WorkbenchInputRespondReq | WorkbenchInputReopenReq = Field(
        default_factory=dict, validate_default=True,
    )

    @classmethod
    def from_mapping(cls, command: Any) -> "WorkbenchCommandReq":
        """HTTP 入口一次完成外壳与参数校验，失败使用统一项目异常。"""
        if not isinstance(command, Mapping):
            raise ValidateErrorException("工作台命令必须是 JSON 对象")
        try:
            return cls.model_validate(dict(command))
        except ValidationError as exc:
            raise ValidateErrorException(f"工作台命令格式错误: {exc}") from exc

    @field_validator("params", mode="before")
    @classmethod
    def parse_params(
        cls, value: Any, info: ValidationInfo,
    ) -> AgentRunInput | WorkbenchInputRespondReq | WorkbenchInputReopenReq:
        """按方法解析唯一参数类型，旧 SDK 的嵌套输入和单条回答在此收敛。"""
        if not isinstance(value, Mapping):
            raise ValueError("命令 params 必须是对象")
        method = info.data.get("method")
        if method == "input.respond":
            try:
                return WorkbenchInputRespondReq.model_validate(value)
            except ValidationError as exc:
                raise ValidateErrorException("表单提交格式错误") from exc
        if method == "input.reopen":
            try:
                return WorkbenchInputReopenReq.model_validate(value)
            except ValidationError as exc:
                raise ValidateErrorException("表单恢复格式错误") from exc
        if method != "run.start":
            raise ValueError("不支持的工作台命令")
        raw = value.get("input", value)
        if not isinstance(raw, Mapping):
            raise ValidateErrorException("run.start 的 input 必须是对象")
        try:
            parsed = WorkbenchCommandInputReq.model_validate(raw)
            if not parsed.messages and not parsed.query:
                raise ValidateErrorException("run.start 需要 input.messages 或 query")
            return parsed.to_run_input()
        except ValidationError as exc:
            raise ValidateErrorException(f"工作台输入格式错误: {exc}") from exc



class WorkbenchEventParams(BaseModel):
    """一条工作台协议事件的公共参数。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    namespace: tuple[StrictStr, ...] = Field(default_factory=tuple)
    timestamp: Any = None
    data: Any = None
    metadata: dict[StrictStr, Any] = Field(default_factory=dict)
    node: StrictStr | None = None


class WorkbenchEvent(BaseModel):
    """经过公开通道投影、可持久化和对外传输的工作台事件。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["event"]
    event_id: StrictStr = Field(min_length=1)
    seq: StrictInt = Field(ge=0)
    method: Literal[*PUBLIC_EVENT_METHODS]
    params: WorkbenchEventParams


class WorkbenchEventStreamReq(BaseModel):
    """SSE 订阅过滤条件。

    ``input`` 和 ``checkpoints`` 是 LangGraph SDK 根订阅的控制 channel。
    工作台当前不发布这两类事件，但必须接受它们，才能兼容官方
    ``useStream`` 的默认订阅集合。
    """

    model_config = ConfigDict(extra="ignore")

    run_id: UUID
    since: StrictInt = Field(default=0, ge=0)
    channels: list[
        Literal[
            "messages",
            "values",
            "tools",
            "lifecycle",
            "input",
            "checkpoints",
            "custom",
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
    ) -> "WorkbenchEventStreamReq":
        payload = dict(body) if isinstance(body, Mapping) else {}
        if last_event_id:
            sequence = _parse_last_event_sequence(last_event_id)
            since = payload.get("since", 0)
            if sequence is not None and type(since) is int and since >= 0:
                payload["since"] = max(since, sequence)
        try:
            return cls.model_validate(payload)
        except ValidationError as exc:
            raise ValidateErrorException(f"工作台订阅参数错误: {exc}") from exc

    def matches(self, event: WorkbenchEvent) -> bool:
        """判断协议事件是否符合订阅过滤条件。"""
        channel = "input" if event.method == "input.requested" else event.method
        if self.channels and channel not in self.channels:
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
    field_errors: dict[str, dict[str, str]] | None = None


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


class WorkbenchRunMetadataResp(BaseModel):
    """工作台状态中的当前 Run 摘要。"""

    run_id: UUID | None = None
    status: str = "idle"
    last_event_sequence: int = Field(default=0, ge=0)


class WorkbenchRunSummaryResp(BaseModel):
    """会话中的运行摘要；不包含执行事件，也不占用事件序号。"""

    run_id: UUID
    message_id: UUID
    input_message_id: str | None = None
    status: str
    last_event_sequence: int = Field(default=0, ge=0)
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


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


__all__ = [
    "PUBLIC_EVENT_METHODS",
    "WorkbenchCommandReq",
    "WorkbenchInputReopenReq",
    "WorkbenchCommandInputReq",
    "WorkbenchCommandMessageReq",
    "WorkbenchEventStreamReq",
    "WorkbenchCommandResultResp",
    "WorkbenchCommandSuccessResp",
    "WorkbenchCommandErrorResp",
    "WorkbenchEvent",
    "WorkbenchEventParams",
    "WorkbenchHistoryMessageResp",
    "WorkbenchMessagePageInfoResp",
    "WorkbenchRunMetadataResp",
    "WorkbenchRunSummaryResp",
    "WorkbenchStateResp",
    "WorkbenchCancelResp",
]
