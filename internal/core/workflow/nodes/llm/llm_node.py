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
from .llm_entity import LLMNodeData


class LLMNode(BaseNode):
    """大语言模型节点

    支持知识库自动整合：输入变量的meta中设置 is_knowledge=true 和 label 后，
    LLM节点会自动将这些知识库输入合并为 reference_info 变量注入prompt上下文，
    空内容自动跳过。用户在提示词中只需写 {{ reference_info }} 即可。
    """

    node_data: LLMNodeData
    account_id: Optional[UUID] = None
    _flask_app: Flask = PrivateAttr(None)

    def __init__(
        self,
        node_data: LLMNodeData,
        account_id: Optional[UUID] = None,
        flask_app: Flask = None,
        **kwargs,
    ):
        """初始化 LLM 节点"""
        super().__init__(node_data=node_data, **kwargs)
        self.account_id = account_id
        self._flask_app = flask_app

    def _build_reference_info(self, inputs_dict: dict) -> str:
        """自动整合知识库输入，将标记为is_knowledge的输入合并为带标签的参考信息字符串"""
        sections = []
        for variable in self.node_data.inputs:
            if not variable.meta.get("is_knowledge", False):
                continue
            value = inputs_dict.get(variable.name, "")
            if not value or (isinstance(value, str) and not value.strip()):
                continue
            label = variable.meta.get("label", variable.name)
            sections.append(f"【{label}】\n{value}")
        return "\n\n".join(sections)

    def invoke(
        self, state: WorkflowState, config: Optional[RunnableConfig] = None
    ) -> WorkflowState:
        """大语言模型节点调用工具，根据输入字段+预设prompt生成对应内容后输出"""
        # 1.提取节点中的输入数据
        start_at = time.perf_counter()
        inputs_dict = extract_variables_from_state(self.node_data.inputs, state)

        # 2.自动整合知识库输入，生成reference_info变量
        reference_info = self._build_reference_info(inputs_dict)
        if reference_info:
            inputs_dict["reference_info"] = reference_info

        # 3.使用jinja2格式模板信息
        template = Template(self.node_data.prompt)
        prompt_value = template.render(**inputs_dict)

        # 4.通过依赖管理器获取language_model_service并加载模型
        from app.http.module import injector
        from internal.service import LanguageModelService

        with self._flask_app.app_context():
            language_model_service = injector.get(LanguageModelService)
            llm = language_model_service.load_language_model(
                self.node_data.language_model_config,
                account_id=self.account_id,
            )

            # 5.使用stream来代替invoke，避免接口长时间未响应超时
            content = ""
            for chunk in llm.stream(prompt_value):
                content += chunk.content
            content = strip_think_tags(content)

        # 6.提取并构建输出数据结构
        outputs = {}
        if self.node_data.outputs:
            outputs[self.node_data.outputs[0].name] = content
        else:
            outputs["output"] = content

        # 7.构建响应状态并返回
        return {
            "node_results": [
                NodeResult(
                    node_data=self.node_data,
                    status=NodeStatus.SUCCEEDED,
                    inputs=inputs_dict,
                    outputs=outputs,
                    latency=(time.perf_counter() - start_at),
                )
            ]
        }
