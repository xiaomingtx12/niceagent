"""意图识别节点实体定义"""
from typing import Any

from pydantic import Field, field_validator

from internal.core.workflow.entities.node_entity import BaseNodeData
from internal.core.workflow.entities.variable_entity import (
    VariableEntity,
    VariableValueType,
)


class IntentClassifierNodeData(BaseNodeData):
    """意图识别节点数据"""

    intents: list[dict[str, str]] = Field(
        default_factory=list,
        description="意图列表，每个意图包含name和description",
    )
    language_model_config: dict[str, Any] = Field(
        alias="model_config",
        default_factory=dict,
    )
    inputs: list[VariableEntity] = Field(default_factory=list)
    outputs: list[VariableEntity] = Field(
        default_factory=lambda: [
            VariableEntity(
                name="intent_name", value={"type": VariableValueType.GENERATED}
            ),
            VariableEntity(
                name="confidence", type="float", value={"type": VariableValueType.GENERATED}
            ),
        ]
    )

    @field_validator("intents", mode="before")
    def validate_intents(cls, value):
        """校验意图列表"""
        if not value or len(value) == 0:
            raise ValueError("至少需要定义一个意图")
        
        # 确保每个意图都有name和description
        for intent in value:
            if not isinstance(intent, dict):
                raise ValueError("意图必须是字典类型")
            if "name" not in intent or "description" not in intent:
                raise ValueError("每个意图必须包含name和description字段")
            if not intent["name"] or not intent["description"]:
                raise ValueError("意图的name和description不能为空")
        
        return value

    @field_validator("outputs", mode="before")
    def validate_outputs(cls, value):
        """固定输出为intent_name和confidence"""
        return [
            VariableEntity(
                name="intent_name", value={"type": VariableValueType.GENERATED}
            ),
            VariableEntity(
                name="confidence", type="float", value={"type": VariableValueType.GENERATED}
            ),
        ]
