from typing import Any, Optional, Iterator

from flask import current_app
from langchain_core.runnables import RunnableConfig
from langchain_core.runnables.utils import Input, Output
from langchain_core.tools import BaseTool
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph
from pydantic import PrivateAttr, BaseModel, Field, create_model

from internal.exception import ValidateErrorException
from .entities.node_entity import NodeType
from .entities.variable_entity import VARIABLE_TYPE_MAP
from .entities.workflow_entity import WorkflowConfig, WorkflowState
from .nodes import (
    StartNode,
    LLMNode,
    TemplateTransformNode,
    DatasetRetrievalNode,
    CodeNode,
    ToolNode,
    HttpRequestNode,
    IntentClassifierNode,
    EndNode,
)

# 节点类映射
NodeClasses = {
    NodeType.START: StartNode,
    NodeType.END: EndNode,
    NodeType.LLM: LLMNode,
    NodeType.TEMPLATE_TRANSFORM: TemplateTransformNode,
    NodeType.DATASET_RETRIEVAL: DatasetRetrievalNode,
    NodeType.CODE: CodeNode,
    NodeType.TOOL: ToolNode,
    NodeType.HTTP_REQUEST: HttpRequestNode,
    NodeType.INTENT_CLASSIFIER: IntentClassifierNode,
}


class Workflow(BaseTool):
    """工作流LangChain工具类"""

    _workflow_config: WorkflowConfig = PrivateAttr(None)
    _workflow: CompiledStateGraph = PrivateAttr(None)

    def __init__(self, workflow_config: WorkflowConfig, **kwargs: Any):
        """构造函数，完成工作流函数的初始化"""
        # 1.调用父类构造函数完成基础数据初始化
        super().__init__(
            name=workflow_config.name,
            description=workflow_config.description,
            args_schema=self._build_args_schema(workflow_config),
            **kwargs,
        )

        # 2.完善工作流配置与工作流图结构程序的初始化
        self._workflow_config = workflow_config
        self._workflow = self._build_workflow()

    @classmethod
    def _build_args_schema(cls, workflow_config: WorkflowConfig) -> type[BaseModel]:
        """构建输入参数结构体"""
        # 1.提取开始节点的输入参数信息
        fields = {}
        inputs = next(
            (
                node.inputs
                for node in workflow_config.nodes
                if node.node_type == NodeType.START
            ),
            [],
        )

        # 2.循环遍历所有输入信息并创建字段映射
        for input in inputs:
            field_name = input.name
            field_type = VARIABLE_TYPE_MAP.get(input.type, str)
            field_required = input.required
            field_description = input.description

            fields[field_name] = (
                field_type if field_required else Optional[field_type],
                Field(description=field_description),
            )

        # 3.调用create_model创建一个BaseModel类，并使用上述分析好的字段
        return create_model("DynamicModel", **fields)

    def _build_workflow(self) -> CompiledStateGraph:
        """构建编译后的工作流图程序"""
        # 1.创建graph图程序结构
        graph = StateGraph(WorkflowState)

        # 2.提取nodes和edges信息
        nodes = self._workflow_config.nodes
        edges = self._workflow_config.edges

        # 3.循环遍历nodes节点信息添加节点
        for node in nodes:
            node_flag = f"{node.node_type.value}_{node.id}"
            if node.node_type == NodeType.START:
                graph.add_node(
                    node_flag,
                    NodeClasses[NodeType.START](node_data=node),
                )
            elif node.node_type == NodeType.LLM:
                graph.add_node(
                    node_flag,
                    NodeClasses[NodeType.LLM](
                        node_data=node,
                        account_id=self._workflow_config.account_id
                    ),
                )
            elif node.node_type == NodeType.TEMPLATE_TRANSFORM:
                graph.add_node(
                    node_flag,
                    NodeClasses[NodeType.TEMPLATE_TRANSFORM](node_data=node),
                )
            elif node.node_type == NodeType.DATASET_RETRIEVAL:
                graph.add_node(
                    node_flag,
                    NodeClasses[NodeType.DATASET_RETRIEVAL](
                        flask_app=current_app._get_current_object(),
                        account_id=self._workflow_config.account_id,
                        node_data=node,
                    ),
                )
            elif node.node_type == NodeType.CODE:
                graph.add_node(
                    node_flag,
                    NodeClasses[NodeType.CODE](node_data=node),
                )
            elif node.node_type == NodeType.TOOL:
                graph.add_node(
                    node_flag,
                    NodeClasses[NodeType.TOOL](node_data=node),
                )
            elif node.node_type == NodeType.HTTP_REQUEST:
                graph.add_node(
                    node_flag,
                    NodeClasses[NodeType.HTTP_REQUEST](node_data=node),
                )
            elif node.node_type == NodeType.INTENT_CLASSIFIER:
                graph.add_node(
                    node_flag,
                    NodeClasses[NodeType.INTENT_CLASSIFIER](
                        node_data=node,
                        account_id=self._workflow_config.account_id
                    ),
                )
            elif node.node_type == NodeType.END:
                graph.add_node(
                    node_flag,
                    NodeClasses[NodeType.END](node_data=node),
                )
            else:
                raise ValidateErrorException("工作流节点类型错误，请核实后重试")

        # 4.分类边：普通边和条件边
        normal_edges = []
        conditional_edges_map = {}  # key: source_node, value: list of edges
        conditional_target_nodes = set()  # 条件边的目标节点集合
        
        for edge in edges:
            if edge.condition:
                # 条件边
                source_node = f"{edge.source_type.value}_{edge.source}"
                if source_node not in conditional_edges_map:
                    conditional_edges_map[source_node] = []
                conditional_edges_map[source_node].append(edge)
                # 记录条件边的目标节点
                conditional_target_nodes.add(f"{edge.target_type.value}_{edge.target}")
            else:
                # 普通边
                normal_edges.append(edge)

        # 5.处理普通边，区分真正的并行汇聚边和条件分支后的汇聚边
        start_node = ""
        end_node = ""
        # 按目标节点分组
        edges_by_target = {}  # key:终点, value:起点列表
        
        for edge in normal_edges:
            source_node = f"{edge.source_type.value}_{edge.source}"
            target_node = f"{edge.target_type.value}_{edge.target}"
            
            if target_node not in edges_by_target:
                edges_by_target[target_node] = [source_node]
            else:
                edges_by_target[target_node].append(source_node)

            # 检测特殊节点
            if edge.source_type == NodeType.START:
                start_node = source_node
            if edge.target_type == NodeType.END:
                end_node = target_node

        # 6.处理条件边
        for source_node, cond_edges in conditional_edges_map.items():
            # 创建条件函数，返回条件字符串（condition_map的key）
            def create_condition_func(edges_list):
                def condition_func(state: WorkflowState) -> str:
                    """根据意图识别结果返回对应的条件分支标识"""
                    intent = state.get("intent_condition", "")
                    # 查找匹配的条件
                    for edge in edges_list:
                        if edge.condition == intent:
                            return edge.condition
                    # 如果没有匹配，返回第一个条件（默认分支）
                    if edges_list:
                        return edges_list[0].condition
                    return "__end__"
                return condition_func
            
            # 构建条件映射：条件字符串 -> 目标节点名
            condition_map = {}
            for edge in cond_edges:
                target_node = f"{edge.target_type.value}_{edge.target}"
                condition_map[edge.condition] = target_node
                
                # 检测特殊节点
                if edge.target_type == NodeType.END:
                    end_node = target_node
            
            # 添加条件边
            graph.add_conditional_edges(
                source_node,
                create_condition_func(cond_edges),
                condition_map
            )

        # 7.设置开始和终点
        graph.set_entry_point(start_node)
        graph.set_finish_point(end_node)

        # 8.添加普通边，区分条件分支汇聚和真正的并行汇聚
        for target_node, source_nodes in edges_by_target.items():
            # 判断源节点是否来自条件分支的目标节点
            # 如果源节点中有任何一个是条件边的目标，说明这是条件分支后的汇聚
            # 此时应该添加独立边（任一分支完成即可触发），而非fan-in边（等待所有分支完成）
            has_conditional_source = any(
                src in conditional_target_nodes for src in source_nodes
            )
            
            if has_conditional_source or len(source_nodes) == 1:
                # 条件分支汇聚或单一来源：添加独立边
                for source_node in source_nodes:
                    graph.add_edge(source_node, target_node)
            else:
                # 真正的并行汇聚：使用fan-in边（等待所有源节点完成）
                graph.add_edge(source_nodes, target_node)

        # 9.构建图程序并编译
        return graph.compile()

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        """工作流组件基础run方法"""
        return self._workflow.invoke({"inputs": kwargs})

    def stream(
        self,
        input: Input,
        config: Optional[RunnableConfig] = None,
        **kwargs: Optional[Any],
    ) -> Iterator[Output]:
        """工作流流式输出每个节点对应的结果"""
        return self._workflow.stream({"inputs": input})
