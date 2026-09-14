"""AppConfig 草稿更新请求的结构化校验。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from internal.entity.subagent_config import SubAgentConfig
from internal.entity.audio_entity import ALLOWED_AUDIO_VOICES
from internal.exception import ValidateErrorException
from internal.schema.skill.skill_schema import SkillBindingReq


class AppModelConfigReq(BaseModel):
    """草稿更新中的模型引用和参数。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: StrictStr = Field(min_length=1)
    model: StrictStr = Field(min_length=1)
    parameters: dict[StrictStr, Any]
    provider_connection_id: UUID | None = None
    features: list[StrictStr] | None = None
    modalities: list[StrictStr] | None = None
    input_modalities: list[StrictStr] | None = None
    output_modalities: list[StrictStr] | None = None
    native_file_input: StrictBool | None = None


class AppToolBindingReq(BaseModel):
    """草稿中保存的一个工具引用。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: Literal["builtin_tool", "api_tool", "mcp_tool"]
    provider_id: StrictStr = Field(min_length=1)
    tool_id: StrictStr = Field(min_length=1)
    params: dict[StrictStr, Any]


class AppToolBindingListReq(RootModel[list[AppToolBindingReq]]):
    """工具绑定列表的数量和唯一性约束。"""

    @model_validator(mode="after")
    def validate_bindings(self) -> "AppToolBindingListReq":
        if len(self.root) > 5:
            raise ValueError("Agent 绑定的工具数不能超过 5")
        keys = [(item.provider_id, item.tool_id) for item in self.root]
        if len(keys) != len(set(keys)):
            raise ValueError("绑定插件存在重复")
        return self


class AppWorkflowBindingListReq(RootModel[list[UUID]]):
    """工作流引用列表的数量和唯一性约束。"""

    @model_validator(mode="after")
    def validate_bindings(self) -> "AppWorkflowBindingListReq":
        if len(self.root) > 5:
            raise ValueError("Agent 绑定的工作流数量不能超过 5 个")
        if len(self.root) != len(set(self.root)):
            raise ValueError("绑定工作流存在重复")
        return self


class AppDatasetBindingListReq(RootModel[list[UUID]]):
    """知识库引用列表的数量和唯一性约束。"""

    @model_validator(mode="after")
    def validate_bindings(self) -> "AppDatasetBindingListReq":
        if len(self.root) > 5:
            raise ValueError("Agent 绑定的知识库数量不能超过 5 个")
        if len(self.root) != len(set(self.root)):
            raise ValueError("绑定知识库存在重复")
        return self


class RetrievalConfigReq(BaseModel):
    """知识库检索参数。"""

    model_config = ConfigDict(extra="forbid")

    retrieval_strategy: Literal["semantic", "full_text", "hybrid"]
    k: Annotated[StrictInt, Field(ge=0, le=10)]
    score: Annotated[float, Field(strict=True, ge=0, le=1)]


class LongTermMemoryConfigReq(BaseModel):
    """长期记忆开关。"""

    model_config = ConfigDict(extra="forbid")

    enable: StrictBool = False


class ToggleConfigReq(BaseModel):
    """只有启用开关的配置。"""

    model_config = ConfigDict(extra="forbid")

    enable: StrictBool


class TextToSpeechConfigReq(ToggleConfigReq):
    """文本转语音配置。"""

    voice: StrictStr
    auto_play: StrictBool

    @field_validator("voice")
    @classmethod
    def validate_voice(cls, value: str) -> str:
        if value not in ALLOWED_AUDIO_VOICES:
            raise ValueError("不支持的文本转语音音色")
        return value


class ReviewInputConfigReq(ToggleConfigReq):
    """输入审核策略。"""

    preset_response: StrictStr = ""

    @field_validator("preset_response", mode="before")
    @classmethod
    def normalize_null_preset_response(cls, value: Any) -> Any:
        """兼容旧配置和前端传入的 null；启用审核时再由组合校验拦截空值。"""
        return "" if value is None else value


class ReviewConfigReq(BaseModel):
    """输入输出审核配置及其组合约束。"""

    model_config = ConfigDict(extra="forbid")

    enable: StrictBool
    keywords: list[StrictStr] = Field(max_length=100)
    inputs_config: ReviewInputConfigReq
    outputs_config: ToggleConfigReq

    @model_validator(mode="after")
    def validate_enabled_policy(self) -> "ReviewConfigReq":
        if not self.enable:
            return self
        if not self.keywords:
            raise ValueError("启用审核时 keywords 不能为空")
        if not self.inputs_config.enable and not self.outputs_config.enable:
            raise ValueError("输入审核和输出审核至少需要开启一项")
        if self.inputs_config.enable and not self.inputs_config.preset_response.strip():
            raise ValueError("输入审核预设响应不能为空")
        return self


class AgentAppResourceReq(BaseModel):
    """运行配置中已完成租户校验的资源引用。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    id: UUID


class AgentAppToolProviderReq(BaseModel):
    """运行配置中的工具提供方快照。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    id: StrictStr = Field(min_length=1)


class AgentAppToolDefinitionReq(BaseModel):
    """运行配置中的工具定义快照。"""

    model_config = ConfigDict(extra="allow", frozen=True)

    id: StrictStr = Field(min_length=1)
    name: StrictStr = Field(min_length=1)
    params: dict[StrictStr, Any] = Field(default_factory=dict)


class AgentAppToolReq(BaseModel):
    """运行时可装配的一个已授权工具。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["builtin_tool", "api_tool", "mcp_tool"]
    provider: AgentAppToolProviderReq
    tool: AgentAppToolDefinitionReq


class AgentAppConfigReq(BaseModel):
    """从 AppConfig Service 交给 Agent 装配服务的可信运行配置。"""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    id: UUID | None = None
    model_config_data: AppModelConfigReq | None = Field(
        default=None,
        alias="model_config",
    )
    preset_prompt: StrictStr = Field(default="", max_length=2000)
    skills: tuple[SkillBindingReq, ...] = Field(default_factory=tuple)
    subagents: tuple[SubAgentConfig, ...] = Field(default_factory=tuple)
    tools: tuple[AgentAppToolReq, ...] = Field(default_factory=tuple)
    workflows: tuple[AgentAppResourceReq, ...] = Field(
        default_factory=tuple,
        max_length=5,
    )
    datasets: tuple[AgentAppResourceReq, ...] = Field(
        default_factory=tuple,
        max_length=5,
    )
    retrieval_config: RetrievalConfigReq = Field(
        default_factory=lambda: RetrievalConfigReq(
            retrieval_strategy="semantic",
            k=10,
            score=0.5,
        )
    )
    long_term_memory: LongTermMemoryConfigReq = Field(
        default_factory=LongTermMemoryConfigReq
    )
    opening_statement: StrictStr = Field(default="", max_length=2000)
    opening_questions: tuple[StrictStr, ...] = Field(
        default_factory=tuple,
        max_length=3,
    )
    user_input: ToggleConfigReq = Field(
        default_factory=lambda: ToggleConfigReq(enable=True)
    )
    speech_to_text: ToggleConfigReq = Field(
        default_factory=lambda: ToggleConfigReq(enable=False)
    )
    text_to_speech: TextToSpeechConfigReq = Field(
        default_factory=lambda: TextToSpeechConfigReq(
            enable=False,
            voice="xiaoxiao",
            auto_play=False,
        )
    )
    suggested_after_answer: ToggleConfigReq = Field(
        default_factory=lambda: ToggleConfigReq(enable=True)
    )
    review_config: ReviewConfigReq = Field(
        default_factory=lambda: ReviewConfigReq(
            enable=False,
            keywords=[],
            inputs_config=ReviewInputConfigReq(
                enable=False,
                preset_response="",
            ),
            outputs_config=ToggleConfigReq(enable=False),
        )
    )
    updated_at: StrictInt | None = None
    created_at: StrictInt | None = None

    @field_validator("model_config_data", mode="before")
    @classmethod
    def normalize_empty_model_config(cls, value: Any) -> Any:
        """无外部模型的内置 Agent 允许使用空模型配置。"""
        return None if value == {} else value

    @model_validator(mode="after")
    def validate_unique_runtime_bindings(self) -> "AgentAppConfigReq":
        """拒绝运行配置中重复的 Skill、SubAgent 和资源引用。"""
        groups = (
            ("Skill", [item.name for item in self.skills]),
            ("SubAgent", [item.name for item in self.subagents]),
            (
                "工具",
                [(item.provider.id, item.tool.name) for item in self.tools],
            ),
            ("工作流", [item.id for item in self.workflows]),
            ("知识库", [item.id for item in self.datasets]),
        )
        for label, values in groups:
            if len(values) != len(set(values)):
                raise ValueError(f"{label}运行绑定不能重复")
        return self

    def dump_tools(self) -> list[dict[str, Any]]:
        """转换为现有工具加载适配器使用的 JSON 结构。"""
        return [tool.model_dump(mode="json") for tool in self.tools]


class DraftAppConfigPatchReq(BaseModel):
    """应用草稿配置的部分更新请求。"""

    model_config = ConfigDict(extra="forbid")

    model_config_data: AppModelConfigReq | None = Field(
        default=None,
        alias="model_config",
    )
    preset_prompt: StrictStr | None = Field(default=None, max_length=2000)
    skills: list[SkillBindingReq] | None = None
    subagents: list[SubAgentConfig] | None = None
    tools: AppToolBindingListReq | None = None
    workflows: AppWorkflowBindingListReq | None = None
    datasets: AppDatasetBindingListReq | None = None
    retrieval_config: RetrievalConfigReq | None = None
    long_term_memory: LongTermMemoryConfigReq | None = None
    opening_statement: StrictStr | None = Field(default=None, max_length=2000)
    opening_questions: list[StrictStr] | None = Field(default=None, max_length=3)
    user_input: ToggleConfigReq | None = None
    speech_to_text: ToggleConfigReq | None = None
    text_to_speech: TextToSpeechConfigReq | None = None
    suggested_after_answer: ToggleConfigReq | None = None
    review_config: ReviewConfigReq | None = None

    @field_validator("model_config_data", mode="before")
    @classmethod
    def normalize_empty_model_config(cls, value: Any) -> Any:
        """允许前端在尚未选择模型时提交空模型配置。

        新建 Agent 的默认配置中模型暂时为空，空对象不应被当作一个
        ``AppModelConfigReq`` 进行字段校验；真正运行或读取配置时会由
        AppConfigService 解析并补充当前账号的默认模型。
        """
        return None if value == {} else value

    @model_validator(mode="before")
    @classmethod
    def reject_null_fields(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            null_fields = [key for key, item in value.items() if item is None]
            if null_fields:
                raise ValueError("配置字段不能为 null: " + ", ".join(null_fields))
        return value

    @model_validator(mode="after")
    def require_update_field(self) -> "DraftAppConfigPatchReq":
        if not self.model_fields_set:
            raise ValueError("草稿配置不能为空")
        return self

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DraftAppConfigPatchReq":
        """解析草稿更新，并映射为项目统一校验异常。"""
        try:
            return cls.model_validate(dict(value))
        except ValidationError as exc:
            raise ValidateErrorException(f"草稿配置格式错误: {exc}") from exc


__all__ = [
    "AgentAppConfigReq",
    "AgentAppResourceReq",
    "AgentAppToolReq",
    "AppDatasetBindingListReq",
    "AppModelConfigReq",
    "AppToolBindingListReq",
    "AppToolBindingReq",
    "AppWorkflowBindingListReq",
    "DraftAppConfigPatchReq",
    "LongTermMemoryConfigReq",
]
