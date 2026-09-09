from copy import deepcopy
from typing import Any

from pydantic import BaseModel, Field

from internal.entity.app_entity import DEFAULT_APP_CONFIG


class BuiltinAppEntity(BaseModel):
    """可复制为应用草稿的内置模板，不包含租户资源或凭据。"""

    id: str = Field(default="")
    category: str = Field(default="")
    name: str = Field(default="")
    icon: str = Field(default="")
    description: str = Field(default="")
    tags: list[str] = Field(default_factory=list)
    requirements: list[str] = Field(default_factory=list)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    subagents: list[dict[str, Any]] = Field(default_factory=list)
    language_model_config: dict[str, Any] = Field(
        default_factory=lambda: deepcopy(DEFAULT_APP_CONFIG.get("model_config"))
    )
    dialog_round: int = Field(default=DEFAULT_APP_CONFIG.get("dialog_round"))
    preset_prompt: str = Field(default=DEFAULT_APP_CONFIG.get("preset_prompt"))
    tools: list[dict[str, Any]] = Field(default_factory=list)
    workflows: list[str] = Field(default_factory=list)
    retrieval_config: dict[str, Any] = Field(
        default_factory=lambda: deepcopy(DEFAULT_APP_CONFIG.get("retrieval_config"))
    )
    long_term_memory: dict[str, Any] = Field(
        default_factory=lambda: deepcopy(DEFAULT_APP_CONFIG.get("long_term_memory"))
    )
    opening_statement: str = Field(default=DEFAULT_APP_CONFIG.get("opening_statement"))
    opening_questions: list[str] = Field(
        default_factory=lambda: deepcopy(DEFAULT_APP_CONFIG.get("opening_questions"))
    )
    user_input: dict[str, Any] = Field(
        default_factory=lambda: dict(DEFAULT_APP_CONFIG["user_input"])
    )
    speech_to_text: dict[str, Any] = Field(
        default_factory=lambda: deepcopy(DEFAULT_APP_CONFIG.get("speech_to_text"))
    )
    text_to_speech: dict[str, Any] = Field(
        default_factory=lambda: deepcopy(DEFAULT_APP_CONFIG.get("text_to_speech"))
    )
    suggested_after_answer: dict[str, Any] = Field(
        default_factory=lambda: deepcopy(
            DEFAULT_APP_CONFIG.get("suggested_after_answer", {"enable": True})
        ),
    )
    review_config: dict[str, Any] = Field(
        default_factory=lambda: deepcopy(DEFAULT_APP_CONFIG.get("review_config"))
    )
    created_at: int = Field(default=0)

    def to_draft_config(self) -> dict[str, Any]:
        """仅输出草稿配置字段，模型由当前账号选择，租户资源须另行绑定。"""
        fields = (set(DEFAULT_APP_CONFIG) | {"suggested_after_answer"}) - {
            "model_config",
            "datasets",
        }
        return self.model_dump(include=fields)
