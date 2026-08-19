from enum import Enum


# 生成icon描述提示词模板
GENERATE_ICON_PROMPT_TEMPLATE = """你是一个拥有10年经验的AI绘画工程师，可以将用户传递的`应用名称`和`应用描述`转换为对应应用的icon描述。
该描述主要用于通义千问AI绘画，用户传递的数据如下:

应用名称: {name}。
应用描述: {description}。

并且除了icon描述提示词外，其他什么都不要生成"""


class AppStatus(str, Enum):
    """应用状态枚举类"""

    DRAFT = "draft"
    PUBLISHED = "published"


class AppConfigType(str, Enum):
    """应用配置类型枚举类"""

    DRAFT = "draft"
    PUBLISHED = "published"


# 应用默认配置信息
DEFAULT_APP_CONFIG = {
    # 模型配置由当前账号已激活的模型注入，不能使用全局固定模型。
    "model_config": {},
    "dialog_round": 3,
    "preset_prompt": "",
    "skills": [],
    "subagents": [],
    "tools": [],
    "workflows": [],
    "datasets": [],
    "retrieval_config": {
        "retrieval_strategy": "semantic",
        "k": 10,
        "score": 0.5,
    },
    "long_term_memory": {
        "enable": True,
    },
    "opening_statement": "",
    "opening_questions": [],
    "speech_to_text": {
        "enable": False,
    },
    "text_to_speech": {
        "enable": False,
        "voice": "xiaoxiao",
        "auto_play": False,
    },
    "review_config": {
        "enable": False,
        "keywords": [],
        "inputs_config": {
            "enable": False,
            "preset_response": "",
        },
        "outputs_config": {
            "enable": False,
        },
    },
}
