import json
import time
from typing import Optional, Any
from uuid import UUID

from flask import Flask
from jinja2 import Template
from langchain_core.runnables import RunnableConfig
from pydantic import PrivateAttr

from internal.core.workflow.entities.node_entity import NodeResult, NodeStatus
from internal.core.workflow.entities.workflow_entity import WorkflowState
from internal.core.workflow.nodes import BaseNode
from internal.core.workflow.utils.helper import extract_variables_from_state, strip_think_tags
from .intent_classifier_entity import IntentClassifierNodeData

INTENT_CLASSIFICATION_PROMPT = """你是一个意图识别助手。请根据用户的输入，判断用户的意图属于以下哪一类。

可用的意图分类：
{% for intent in intents %}
{{ loop.index }}. {{ intent.name }}
   描述：{{ intent.description }}
{% endfor %}

用户输入：{{ user_query }}

请仔细分析用户输入，并返回最匹配的意图名称和置信度（0-1之间的小数）。

你必须严格按照以下JSON格式返回结果，不要包含任何其他内容：
{
    "intent_name": "意图名称",
    "confidence": 0.95
}

注意：
1. intent_name必须是上述意图列表中的一个
2. confidence是0到1之间的小数，表示匹配的置信度
3. 只返回JSON，不要有任何额外的解释或文字
"""


class IntentClassifierNode(BaseNode):
    """意图识别节点"""

    node_data: IntentClassifierNodeData
    account_id: Optional[UUID] = None
    _flask_app: Flask = PrivateAttr(None)

    def __init__(
        self,
        node_data: IntentClassifierNodeData,
        account_id: Optional[UUID] = None,
        flask_app: Flask = None,
        **kwargs
    ):
        """初始化意图识别节点"""
        super().__init__(node_data=node_data, **kwargs)
        self.account_id = account_id
        self._flask_app = flask_app

    def invoke(
        self, state: WorkflowState, config: Optional[RunnableConfig] = None
    ) -> WorkflowState:
        """意图识别节点，根据用户输入和预定义的意图列表，使用LLM识别用户意图"""
        # 1.提取节点中的输入数据
        start_at = time.perf_counter()
        inputs_dict = extract_variables_from_state(self.node_data.inputs, state)

        # 2.构建用户查询内容
        user_query = self._build_user_query(inputs_dict)

        # 3.使用jinja2格式模板信息
        template = Template(INTENT_CLASSIFICATION_PROMPT)
        prompt = template.render(
            intents=self.node_data.intents, user_query=user_query
        )

        # 4.通过依赖管理器获取language_model_service并加载模型
        from app.http.module import injector
        from internal.service import LanguageModelService

        with self._flask_app.app_context():
            language_model_service = injector.get(LanguageModelService)
            llm = language_model_service.load_language_model(
                self.node_data.language_model_config, account_id=self.account_id
            )

            # 5.使用stream来代替invoke，避免接口长时间未响应超时
            content = ""
            for chunk in llm.stream(prompt):
                content += chunk.content
            content = strip_think_tags(content)

        # 6.解析LLM返回的JSON结果
        intent_name, confidence = self._parse_llm_response(content)

        # 7.提取并构建输出数据结构
        outputs = {
            "intent_name": intent_name,
            "confidence": confidence,
        }

        # 8.构建响应状态并返回（包含条件分支标识）
        return {
            "node_results": [
                NodeResult(
                    node_data=self.node_data,
                    status=NodeStatus.SUCCEEDED,
                    inputs=inputs_dict,
                    outputs=outputs,
                    latency=(time.perf_counter() - start_at),
                )
            ],
            "intent_condition": intent_name,  # 设置条件分支标识
        }

    def _build_user_query(self, inputs_dict: dict) -> str:
        """构建用户查询内容"""
        if len(inputs_dict) == 1:
            return str(list(inputs_dict.values())[0])
        else:
            return "\n".join([f"{k}: {v}" for k, v in inputs_dict.items()])

    def _parse_llm_response(self, content: str) -> tuple[str, float]:
        """解析LLM返回的JSON结果"""
        try:
            # 提取JSON部分
            json_str = self._extract_json(content)
            result = json.loads(json_str)
            intent_name = result.get("intent_name", "unknown")
            confidence = float(result.get("confidence", 0.0))

            # 验证intent_name是否在定义的意图列表中
            intent_name = self._validate_intent_name(intent_name)
            return intent_name, confidence

        except (json.JSONDecodeError, ValueError):
            # JSON解析失败，返回默认值
            return self.node_data.intents[0]["name"], 0.0

    def _extract_json(self, content: str) -> str:
        """从LLM响应中提取JSON字符串"""
        json_str = content.strip()
        if "```json" in json_str:
            json_start = json_str.find("```json") + 7
            json_end = json_str.find("```", json_start)
            json_str = json_str[json_start:json_end].strip()
        elif "```" in json_str:
            json_start = json_str.find("```") + 3
            json_end = json_str.find("```", json_start)
            json_str = json_str[json_start:json_end].strip()
        return json_str

    def _validate_intent_name(self, intent_name: str) -> str:
        """验证并修正意图名称"""
        valid_intent_names = [intent["name"] for intent in self.node_data.intents]
        if intent_name in valid_intent_names:
            return intent_name

        # 尝试模糊匹配
        intent_name_lower = intent_name.lower()
        for valid_name in valid_intent_names:
            if valid_name.lower() in intent_name_lower or intent_name_lower in valid_name.lower():
                return valid_name

        # 使用第一个意图作为默认值
        return valid_intent_names[0]
