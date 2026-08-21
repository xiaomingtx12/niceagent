from abc import ABC
from enum import Enum
from typing import Any, Optional

from langchain_core.language_models import BaseLanguageModel as LCBaseLanguageModel
from langchain_core.messages import HumanMessage
from pydantic import AliasChoices, BaseModel, Field, model_validator


class DefaultModelParameterName(str, Enum):
    """默认的参数名字，一般是所有LLM都有的一些参数"""

    TEMPERATURE = "temperature"  # 温度
    TOP_P = "top_p"  # 核采样率
    PRESENCE_PENALTY = "presence_penalty"  # 存在惩罚
    FREQUENCY_PENALTY = "frequency_penalty"  # 频率惩罚
    MAX_TOKENS = "max_tokens"  # 要生成的内容的最大tokens数


class ModelType(str, Enum):
    """模型的主要用途。"""

    CHAT = "chat"
    COMPLETION = "completion"
    EMBEDDING = "embedding"
    RERANK = "rerank"
    IMAGE_GENERATION = "image_generation"
    VIDEO_GENERATION = "video_generation"
    SPEECH_TO_TEXT = "speech_to_text"
    TEXT_TO_SPEECH = "text_to_speech"


class ModelParameterType(str, Enum):
    """模型参数类型"""

    FLOAT = "float"
    INT = "int"
    STRING = "string"
    BOOLEAN = "boolean"


class ModelParameterOption(BaseModel):
    """模型参数选项配置模型"""

    label: str  # 配置选项标签
    value: Any  # 配置选项对应的值


class ModelParameter(BaseModel):
    """模型参数实体信息"""

    name: str = ""  # 参数名字
    label: str = ""  # 参数标签
    type: ModelParameterType = ModelParameterType.STRING  # 参数的类型
    help: str = ""  # 帮助信息
    required: bool = False  # 是否必填
    default: Optional[Any] = None  # 默认参数值
    min: Optional[float] = None  # 最小值
    max: Optional[float] = None  # 最大值
    precision: int = 2  # 保留小数的位数
    options: list[ModelParameterOption] = Field(default_factory=list)  # 可选的参数配置


class ModelFeature(str, Enum):
    """模型调用能力；输入模态单独由 ModelModality 表达。"""

    TOOL_CALL = "tool_call"  # 工具调用
    AGENT_THOUGHT = "agent_thought"  # 是否支持智能体推理，一般要求参数量比较大，能回答通用型任务，如果不支持推理则会直接生成答案，而不进行中间步骤
    IMAGE_INPUT = "image_input"  # 旧版图片输入标记，兼容历史模型快照
    VIDEO_INPUT = "video_input"  # 旧版视频输入标记，兼容历史模型快照


class ModelModality(str, Enum):
    """模型的输入或输出数据形态。"""

    TEXT = "text"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    VECTOR = "vector"
    RANKING = "ranking"
    FILE = "file"  # 仅用于读取旧快照，新快照使用 native_file_input


def _default_input_modalities(model_type: ModelType) -> list[ModelModality]:
    if model_type == ModelType.SPEECH_TO_TEXT:
        return [ModelModality.AUDIO]
    return [ModelModality.TEXT]


def _default_output_modalities(model_type: ModelType) -> list[ModelModality]:
    mapping = {
        ModelType.EMBEDDING: [ModelModality.VECTOR],
        ModelType.RERANK: [ModelModality.RANKING],
        ModelType.IMAGE_GENERATION: [ModelModality.IMAGE],
        ModelType.VIDEO_GENERATION: [ModelModality.VIDEO],
        ModelType.TEXT_TO_SPEECH: [ModelModality.AUDIO],
    }
    return list(mapping.get(model_type, [ModelModality.TEXT]))


class ModelEntity(BaseModel):
    """语言模型实体，记录模型的相关信息"""

    model_name: str = Field(default="", alias="model")  # 模型名字，使用model作为别名
    label: str = ""  # 模型标签
    model_type: ModelType = ModelType.CHAT  # 模型类型
    is_dynamic: bool = False  # 是否由第三方模型目录动态发现
    features: list[ModelFeature] = Field(default_factory=list)  # 模型特征信息
    input_modalities: list[ModelModality] = Field(
        default_factory=list,
        validation_alias=AliasChoices("input_modalities", "modalities"),
    )
    output_modalities: list[ModelModality] = Field(default_factory=list)
    native_file_input: bool = False
    context_window: int = 0  # 上下文窗口长度(输入+输出的总长度)
    max_output_tokens: int = 0  # 最大输出内容长度(输出)
    attributes: dict[str, Any] = Field(default_factory=dict)  # 模型固定属性字典
    parameters: list[ModelParameter] = Field(
        default_factory=list
    )  # 模型参数字段规则列表，用于记录模型的配置参数
    metadata: dict[str, Any] = Field(
        default_factory=dict
    )  # 模型元数据，用于存储模型的额外数据，例如价格、词表等等信息

    @model_validator(mode="after")
    def normalize_capabilities(self) -> "ModelEntity":
        """兼容旧快照并补齐与模型用途一致的默认输入输出。"""
        input_modalities = list(self.input_modalities)
        if ModelModality.FILE in input_modalities:
            self.native_file_input = True
            input_modalities.remove(ModelModality.FILE)
        if (
            ModelFeature.IMAGE_INPUT in self.features
            and ModelModality.IMAGE not in input_modalities
        ):
            input_modalities.append(ModelModality.IMAGE)
        if (
            ModelFeature.VIDEO_INPUT in self.features
            and ModelModality.VIDEO not in input_modalities
        ):
            input_modalities.append(ModelModality.VIDEO)
        if not input_modalities:
            input_modalities = _default_input_modalities(self.model_type)
        if (
            self.model_type in {ModelType.CHAT, ModelType.COMPLETION}
            and ModelModality.TEXT not in input_modalities
        ):
            input_modalities.insert(0, ModelModality.TEXT)
        self.input_modalities = list(dict.fromkeys(input_modalities))
        canonical_output_types = {
            ModelType.EMBEDDING,
            ModelType.RERANK,
            ModelType.IMAGE_GENERATION,
            ModelType.VIDEO_GENERATION,
            ModelType.SPEECH_TO_TEXT,
            ModelType.TEXT_TO_SPEECH,
        }
        output_modalities = (
            _default_output_modalities(self.model_type)
            if self.model_type in canonical_output_types
            else self.output_modalities or _default_output_modalities(self.model_type)
        )
        self.output_modalities = list(dict.fromkeys(output_modalities))
        return self


class BaseLanguageModel(LCBaseLanguageModel, ABC):
    """基础语言模型"""

    features: list[ModelFeature] = Field(default_factory=list)  # 模型特性
    input_modalities: list[ModelModality] = Field(
        default_factory=lambda: [ModelModality.TEXT],
        validation_alias=AliasChoices("input_modalities", "modalities"),
    )
    output_modalities: list[ModelModality] = Field(
        default_factory=lambda: [ModelModality.TEXT]
    )
    native_file_input: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)  # 模型元数据信息

    def get_pricing(self) -> tuple[float, float, float]:
        """获取LLM对应的价格信息，返回数据格式为(输入价格, 输出价格, 单位)"""
        # 1.计算获取输入价格、输出价格、单位
        input_price = self.metadata.get("pricing", {}).get("input", 0.0)
        output_price = self.metadata.get("pricing", {}).get("output", 0.0)
        unit = self.metadata.get("pricing", {}).get("unit", 0.0)

        # 2.返回数据
        return input_price, output_price, unit

    def convert_to_human_message(
        self,
        query: str,
        image_urls: list[str] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> HumanMessage:
        """按模型输入模态构造标准多模态 content blocks。"""
        blocks: list[dict[str, Any]] = [{"type": "text", "text": query}]
        if (
            ModelModality.IMAGE in self.input_modalities
            or ModelFeature.IMAGE_INPUT in self.features
        ):
            blocks.extend(
                {"type": "image", "url": image_url}
                for image_url in image_urls or []
            )

        for attachment in attachments or []:
            media_type = str(attachment.get("mime_type", "")).split("/", 1)[0]
            url = str(attachment.get("url") or attachment.get("path") or "")
            if (
                media_type == "image"
                and (
                    ModelModality.IMAGE in self.input_modalities
                    or ModelFeature.IMAGE_INPUT in self.features
                )
            ):
                blocks.append({"type": "image", "url": url})
            elif (
                media_type == "video"
                and (
                    ModelModality.VIDEO in self.input_modalities
                    or ModelFeature.VIDEO_INPUT in self.features
                )
            ):
                blocks.append({"type": "video", "url": url})
            elif (
                media_type == "audio"
                and ModelModality.AUDIO in self.input_modalities
            ):
                blocks.append({"type": "audio", "url": url})
            elif self.native_file_input and url:
                blocks.append(
                    {
                        "type": "file",
                        "url": url,
                        "mime_type": str(attachment.get("mime_type") or ""),
                    }
                )
            else:
                name = str(attachment.get("name") or "附件")
                path = str(attachment.get("path") or "")
                blocks.append(
                    {
                        "type": "text",
                        "text": (
                            f"用户提供了附件「{name}」，文件已保存到 {path}。"
                            "需要查看内容时，请使用 read_file 读取该路径。"
                        ),
                    }
                )

        if len(blocks) == 1:
            return HumanMessage(content=query)
        return HumanMessage(content=blocks)
