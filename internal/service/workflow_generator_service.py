"""工作流生成服务"""

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from injector import inject
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate

from internal.core.language_model import LanguageModelManager
from internal.core.workflow.entities.edge_entity import BaseEdgeData
from internal.core.workflow.entities.node_entity import NodeType, BaseNodeData
from internal.core.workflow.nodes import (
    CodeNodeData,
    DatasetRetrievalNodeData,
    EndNodeData,
    HttpRequestNodeData,
    IntentClassifierNodeData,
    LLMNodeData,
    StartNodeData,
    TemplateTransformNodeData,
    ToolNodeData,
)
from internal.entity.workflow_entity import (
    WorkflowStatus,
    DEFAULT_WORKFLOW_CONFIG,
    WORKFLOW_GENERATION_PROMPT_TEMPLATE,
)
from internal.exception import ValidateErrorException, FailException
from internal.lib.helper import convert_model_to_dict
from internal.model import Account, Workflow
from pkg.sqlalchemy import SQLAlchemy
from .base_service import BaseService


@inject
@dataclass
class WorkflowGeneratorService(BaseService):
    """工作流生成服务"""

    db: SQLAlchemy
    language_model_manager: LanguageModelManager

    def generate_workflow_from_requirements(
        self,
        name: str,
        description: str,
        requirements: str,
        account: Account,
        node_types: list[str] = None,
    ) -> Workflow:
        """根据需求生成工作流

        Args:
            name: 工作流名称
            description: 工作流描述
            requirements: 详细需求说明
            account: 用户账号
            node_types: 可选的节点类型列表

        Returns:
            生成的工作流对象
        """
        # 1. 使用LLM生成工作流配置
        workflow_config = self.generate_workflow_config_with_llm(
            name=name,
            description=description,
            requirements=requirements,
            account=account,
            node_types=node_types,
        )

        # 2. 校验并修复配置
        validated_config = self._validate_and_fix_workflow_config(
            workflow_config, account
        )

        # 3. 生成唯一的工具调用名称
        tool_call_name = self._generate_unique_tool_call_name(name, account)

        # 4. 创建工作流记录
        workflow = self.create(
            Workflow,
            **{
                "account_id": account.id,
                "name": name,
                "description": description,
                "tool_call_name": tool_call_name,
                "status": WorkflowStatus.DRAFT,
                "is_debug_passed": False,
                **DEFAULT_WORKFLOW_CONFIG,
                "draft_graph": {
                    "nodes": validated_config["nodes"],
                    "edges": validated_config["edges"],
                },
            },
        )

        return workflow

    def generate_workflow_config_with_llm(
        self,
        name: str,
        description: str,
        requirements: str,
        account: Account,
        node_types: list[str] = None,
    ) -> dict[str, Any]:
        """使用LLM生成工作流配置

        Args:
            name: 工作流名称
            description: 工作流描述
            requirements: 详细需求说明
            account: 用户账号
            node_types: 可选的节点类型列表

        Returns:
            工作流配置字典
        """
        # 1. 获取可用的节点类型
        available_node_types = self._get_available_node_types_description()

        # 2. 获取示例配置
        example_config = self._get_example_workflow_config()

        # 3. 构建提示词
        prompt_template = ChatPromptTemplate.from_template(
            WORKFLOW_GENERATION_PROMPT_TEMPLATE
        )

        # 4. 获取LLM（使用通义千问）
        provider = self.language_model_manager.get_provider("tongyi")
        if not provider:
            raise FailException("未找到通义千问模型提供者")

        model_entity = provider.get_model_entity("qwen-max")
        if not model_entity:
            raise FailException("未找到qwen-max模型")

        # 获取模型类并实例化
        model_class = provider.get_model_class(model_entity.model_type)

        # 获取用户的 API Key
        from internal.lib.api_key_helper import get_api_key_for_provider

        api_key = get_api_key_for_provider("tongyi", account.id)

        llm = model_class(
            **model_entity.attributes,
            dashscope_api_key=api_key,
            features=model_entity.features,
            metadata=model_entity.metadata,
        )

        # 5. 构建生成链
        generation_chain = prompt_template | llm | StrOutputParser()

        # 6. 调用LLM生成配置
        try:
            llm_response = generation_chain.invoke(
                {
                    "name": name,
                    "description": description,
                    "requirements": requirements,
                    "available_node_types": available_node_types,
                    "example_config": example_config,
                    "node_types_filter": (
                        f"只使用以下节点类型: {', '.join(node_types)}"
                        if node_types
                        else "可以使用所有可用的节点类型"
                    ),
                }
            )

            # 7. 解析JSON响应
            # 提取JSON部分（可能包含在```json```代码块中）
            if "```json" in llm_response:
                json_start = llm_response.find("```json") + 7
                json_end = llm_response.find("```", json_start)
                json_str = llm_response[json_start:json_end].strip()
            elif "```" in llm_response:
                json_start = llm_response.find("```") + 3
                json_end = llm_response.find("```", json_start)
                json_str = llm_response[json_start:json_end].strip()
            else:
                json_str = llm_response.strip()

            workflow_config = json.loads(json_str)

            return workflow_config

        except json.JSONDecodeError as e:
            logging.error(f"解析LLM生成的工作流配置失败: {str(e)}")
            logging.error(f"LLM响应: {llm_response}")
            raise FailException("生成工作流配置失败，请重试")
        except Exception as e:
            logging.error(f"生成工作流配置时发生错误: {str(e)}")
            raise FailException(f"生成工作流配置失败: {str(e)}")

    # 节点类型与节点数据类映射
    NODE_DATA_CLASSES = {
        NodeType.START: StartNodeData,
        NodeType.END: EndNodeData,
        NodeType.LLM: LLMNodeData,
        NodeType.TEMPLATE_TRANSFORM: TemplateTransformNodeData,
        NodeType.DATASET_RETRIEVAL: DatasetRetrievalNodeData,
        NodeType.CODE: CodeNodeData,
        NodeType.TOOL: ToolNodeData,
        NodeType.HTTP_REQUEST: HttpRequestNodeData,
        NodeType.INTENT_CLASSIFIER: IntentClassifierNodeData,
    }

    def _validate_and_fix_workflow_config(
        self, workflow_config: dict[str, Any], account: Account
    ) -> dict[str, Any]:
        """校验并修复工作流配置

        LLM生成的工作流配置通常缺少很多字段（meta、description、value等），
        需要通过Pydantic实体类进行规范化，自动填充默认值，确保数据结构与
        前端手动创建的工作流一致。

        Args:
            workflow_config: 工作流配置
            account: 用户账号

        Returns:
            校验并修复后的配置
        """
        # 1. 确保包含nodes和edges
        if "nodes" not in workflow_config or "edges" not in workflow_config:
            raise ValidateErrorException("工作流配置必须包含nodes和edges")

        nodes = workflow_config["nodes"]
        edges = workflow_config["edges"]

        # 2. 校验并修复节点
        validated_nodes = []
        node_id_map = {}  # 旧ID -> 新ID映射

        for i, node in enumerate(nodes):
            try:
                # 2.1 确保节点有必填字段
                if "node_type" not in node:
                    continue

                # 2.2 生成或验证节点ID
                old_id = node.get("id")
                if not old_id or not isinstance(old_id, str):
                    new_id = str(uuid.uuid4())
                else:
                    new_id = old_id

                node_id_map[old_id] = new_id
                node["id"] = new_id

                # 2.3 确保有title
                if "title" not in node or not node["title"]:
                    node["title"] = f"{node['node_type']}节点"

                # 2.4 确保有description
                if "description" not in node:
                    node["description"] = ""

                # 2.5 确保有position
                if "position" not in node:
                    node["position"] = {"x": 100 + i * 250, "y": 200}

                # 2.6 确保有inputs和outputs
                if "inputs" not in node:
                    node["inputs"] = []
                if "outputs" not in node:
                    node["outputs"] = []

                # 2.7 知识库检索节点：LLM可能不会生成dataset_ids，补充空列表
                if node["node_type"] == "dataset_retrieval":
                    if "dataset_ids" not in node:
                        node["dataset_ids"] = []

                validated_nodes.append(node)

            except Exception as e:
                logging.warning(f"跳过无效节点: {str(e)}")
                continue

        # 3. 校验并修复边
        validated_edges = []
        for i, edge in enumerate(edges):
            try:
                # 3.1 更新source和target ID
                old_source = edge.get("source")
                old_target = edge.get("target")

                if old_source not in node_id_map or old_target not in node_id_map:
                    continue

                edge["source"] = node_id_map[old_source]
                edge["target"] = node_id_map[old_target]

                # 3.2 生成边ID
                if "id" not in edge:
                    edge["id"] = f"edge-{i+1:03d}"

                # 3.3 确保有source_type和target_type
                source_node = next(
                    (n for n in validated_nodes if n["id"] == edge["source"]), None
                )
                target_node = next(
                    (n for n in validated_nodes if n["id"] == edge["target"]), None
                )

                if source_node and target_node:
                    edge["source_type"] = source_node["node_type"]
                    edge["target_type"] = target_node["node_type"]
                    validated_edges.append(edge)

            except Exception as e:
                logging.warning(f"跳过无效边: {str(e)}")
                continue

        # 4. 通过Pydantic实体类规范化节点数据，自动填充缺失字段的默认值
        #    这一步确保LLM生成的数据与前端手动创建的数据结构一致：
        #    - inputs/outputs中的每个变量会补充meta、description、required、value等字段
        #    - model_config会被正确转换为language_model_config（通过alias机制）
        #    - outputs的validator会强制覆盖为标准输出（如LLM节点固定输出output）
        normalized_nodes = []
        for node in validated_nodes:
            try:
                node_type = node.get("node_type", "")
                node_data_cls = self.NODE_DATA_CLASSES.get(node_type)
                if node_data_cls is None:
                    logging.warning(f"未知节点类型: {node_type}，跳过规范化")
                    normalized_nodes.append(node)
                    continue

                # 通过Pydantic实体类实例化，触发字段校验和默认值填充
                node_data = node_data_cls(**node)
                # 序列化回字典，UUID/Enum等类型会被转换为可JSON序列化的格式
                normalized_nodes.append(convert_model_to_dict(node_data))
            except Exception as e:
                logging.warning(f"节点规范化失败({node.get('title', '')}): {str(e)}，使用原始数据")
                normalized_nodes.append(node)

        # 5. 通过Pydantic实体类规范化边数据
        normalized_edges = []
        for edge in validated_edges:
            try:
                edge_data = BaseEdgeData(**edge)
                normalized_edges.append(convert_model_to_dict(edge_data))
            except Exception as e:
                logging.warning(f"边规范化失败: {str(e)}，使用原始数据")
                normalized_edges.append(edge)

        return {"nodes": normalized_nodes, "edges": normalized_edges}

    def _generate_unique_tool_call_name(self, name: str, account: Account) -> str:
        """生成唯一的工具调用名称

        Args:
            name: 工作流名称
            account: 用户账号

        Returns:
            唯一的工具调用名称
        """
        # 1. 基础名称：转换为小写，替换空格为下划线
        base_name = name.lower().replace(" ", "_").replace("-", "_")

        # 2. 移除特殊字符，只保留字母、数字和下划线
        import re

        base_name = re.sub(r"[^a-z0-9_]", "", base_name)

        # 3. 如果名称为空（例如纯中文名称），使用随机名称作为兜底
        if not base_name or base_name.strip("_") == "":
            base_name = f"workflow_{uuid.uuid4().hex[:8]}"

        # 4. 检查是否已存在
        tool_call_name = base_name
        counter = 1

        while True:
            existing = (
                self.db.session.query(Workflow)
                .filter(
                    Workflow.tool_call_name == tool_call_name,
                    Workflow.account_id == account.id,
                )
                .first()
            )

            if not existing:
                break

            tool_call_name = f"{base_name}_{counter}"
            counter += 1

        return tool_call_name

    def _get_available_node_types_description(self) -> str:
        """获取可用节点类型的描述

        Returns:
            节点类型描述字符串
        """
        node_types_desc = """
## 可用节点类型

1. **start** - 开始节点
   - 接收工作流的输入参数
   - 每个工作流必须有且只有一个开始节点
   - 输出变量名：自定义（在inputs中定义）

2. **llm** - 大语言模型节点
   - 调用LLM进行文本生成、分析等
   - 需要配置prompt和model_config
   - 输出变量名：output

3. **template_transform** - 模板转换节点
   - 使用Jinja2模板格式化输出
   - 需要配置template字符串
   - 输出变量名：output

4. **dataset_retrieval** - 知识库检索节点
   - 从知识库中检索相关文档
   - 需要配置dataset_ids和retrieval_config
   - 输出变量名：combine_documents

5. **http_request** - HTTP请求节点
   - 调用外部API获取数据
   - 需要配置url和method
   - 输出变量名：text, status_code

6. **code** - 代码执行节点
   - 执行Python代码进行数据处理
   - 需要配置code字符串
   - 输出变量名：自定义（在outputs中定义）

7. **tool** - 工具调用节点
   - 调用系统内置工具或API工具
   - 需要配置provider_id和tool_id
   - 输出变量名：text

8. **end** - 结束节点
   - 输出工作流的最终结果
   - 每个工作流必须有且只有一个结束节点
   - 通过outputs引用前面节点的输出
"""
        return node_types_desc

    def _get_example_workflow_config(self) -> str:
        """获取示例工作流配置

        Returns:
            示例配置JSON字符串
        """
        example = {
            "nodes": [
                {
                    "id": "start-001",
                    "node_type": "start",
                    "title": "开始",
                    "description": "接收输入",
                    "position": {"x": 100, "y": 200},
                    "inputs": [
                        {
                            "name": "user_input",
                            "type": "string",
                            "required": True,
                            "description": "用户输入",
                        }
                    ],
                },
                {
                    "id": "llm-001",
                    "node_type": "llm",
                    "title": "分析处理",
                    "description": "使用LLM分析输入",
                    "position": {"x": 350, "y": 200},
                    "prompt": "请分析以下内容：{{ user_input }}",
                    "model_config": {
                        "provider": "tongyi",
                        "model": "qwen-plus",
                        "parameters": {"temperature": 0.7, "max_tokens": 2000},
                    },
                    "inputs": [
                        {
                            "name": "user_input",
                            "type": "string",
                            "required": True,
                            "value": {
                                "type": "ref",
                                "content": {
                                    "ref_node_id": "start-001",
                                    "ref_var_name": "user_input",
                                },
                            },
                        }
                    ],
                    "outputs": [
                        {
                            "name": "output",
                            "type": "string",
                            "value": {"type": "generated", "content": ""},
                        }
                    ],
                },
                {
                    "id": "end-001",
                    "node_type": "end",
                    "title": "结束",
                    "description": "输出结果",
                    "position": {"x": 600, "y": 200},
                    "outputs": [
                        {
                            "name": "result",
                            "type": "string",
                            "value": {
                                "type": "ref",
                                "content": {
                                    "ref_node_id": "llm-001",
                                    "ref_var_name": "output",
                                },
                            },
                        }
                    ],
                },
            ],
            "edges": [
                {
                    "id": "edge-001",
                    "source": "start-001",
                    "target": "llm-001",
                    "source_type": "start",
                    "target_type": "llm",
                },
                {
                    "id": "edge-002",
                    "source": "llm-001",
                    "target": "end-001",
                    "source_type": "llm",
                    "target_type": "end",
                },
            ],
        }

        return json.dumps(example, indent=2, ensure_ascii=False)
