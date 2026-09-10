import uuid
from dataclasses import dataclass
from datetime import datetime

from injector import inject

from internal.core.builtin_workflows import BuiltinWorkflowManager
from internal.core.builtin_workflows.entities.builtin_workflow_entity import (
    BuiltinWorkflowEntity,
)
from internal.core.builtin_workflows.entities.category_entity import (
    WorkflowCategoryEntity,
)
from internal.entity.workflow_entity import WorkflowStatus
from internal.exception import NotFoundException
from internal.model import Account, Workflow
from pkg.sqlalchemy import SQLAlchemy

from internal.service.common.base_service import BaseService


@inject
@dataclass
class BuiltinWorkflowService(BaseService):
    """内置工作流模板服务"""

    db: SQLAlchemy
    builtin_workflow_manager: BuiltinWorkflowManager

    def get_categories(self) -> list[WorkflowCategoryEntity]:
        """获取工作流模板分类列表信息"""
        return self.builtin_workflow_manager.get_categories()

    def get_builtin_workflows(self) -> list[BuiltinWorkflowEntity]:
        """获取所有内置工作流模板列表"""
        return self.builtin_workflow_manager.get_builtin_workflows()

    def get_builtin_workflow_detail(
        self, builtin_workflow_id: str
    ) -> BuiltinWorkflowEntity:
        """获取内置工作流模板详情"""
        builtin_workflow = self.builtin_workflow_manager.get_builtin_workflow(
            builtin_workflow_id
        )
        if not builtin_workflow:
            raise NotFoundException("该工作流模板不存在，请核实后重试")
        return builtin_workflow

    def create_workflow_from_template(
        self, builtin_workflow_id: str, name: str, account: Account
    ) -> Workflow:
        """从模板创建工作流实例"""
        # 1.获取内置工作流模板信息，并检测是否存在
        builtin_workflow = self.builtin_workflow_manager.get_builtin_workflow(
            builtin_workflow_id
        )
        if not builtin_workflow:
            raise NotFoundException("该工作流模板不存在，请核实后重试")

        # 2.为节点生成新的UUID映射
        node_id_mapping = {}
        new_nodes = []
        for node in builtin_workflow.nodes:
            old_id = node.get("id")
            new_id = str(uuid.uuid4())
            node_id_mapping[old_id] = new_id

            # 复制节点并更新ID
            new_node = {**node, "id": new_id}
            new_nodes.append(new_node)

        # 3.更新节点内部inputs和outputs中的ref_node_id引用
        for node in new_nodes:
            # 3.1 更新inputs中的ref_node_id引用
            if "inputs" in node and isinstance(node["inputs"], list):
                for input_var in node["inputs"]:
                    if (
                        isinstance(input_var, dict)
                        and "value" in input_var
                        and isinstance(input_var["value"], dict)
                        and input_var["value"].get("type") == "ref"
                        and "content" in input_var["value"]
                        and isinstance(input_var["value"]["content"], dict)
                    ):
                        old_ref_id = input_var["value"]["content"].get("ref_node_id")
                        if old_ref_id and old_ref_id in node_id_mapping:
                            input_var["value"]["content"]["ref_node_id"] = (
                                node_id_mapping[old_ref_id]
                            )

            # 3.2 更新outputs中的ref_node_id引用
            if "outputs" in node and isinstance(node["outputs"], list):
                for output_var in node["outputs"]:
                    if (
                        isinstance(output_var, dict)
                        and "value" in output_var
                        and isinstance(output_var["value"], dict)
                        and output_var["value"].get("type") == "ref"
                        and "content" in output_var["value"]
                        and isinstance(output_var["value"]["content"], dict)
                    ):
                        old_ref_id = output_var["value"]["content"].get("ref_node_id")
                        if old_ref_id and old_ref_id in node_id_mapping:
                            output_var["value"]["content"]["ref_node_id"] = (
                                node_id_mapping[old_ref_id]
                            )

        # 4.更新边中的source和target为新的UUID
        new_edges = []
        for edge in builtin_workflow.edges:
            old_source = edge.get("source")
            old_target = edge.get("target")

            new_edge = {
                **edge,
                "id": str(uuid.uuid4()),
                "source": node_id_mapping.get(old_source, old_source),
                "target": node_id_mapping.get(old_target, old_target),
            }
            new_edges.append(new_edge)

        # 5.创建工作流记录
        with self.db.auto_commit():
            workflow = Workflow(
                account_id=account.id,
                name=name,
                tool_call_name=builtin_workflow.tool_call_name,
                icon=builtin_workflow.icon,
                description=builtin_workflow.description,
                draft_graph={"nodes": new_nodes, "edges": new_edges},
                graph={},
                is_debug_passed=False,
                status=WorkflowStatus.DRAFT,
                published_at=None,
            )
            self.db.session.add(workflow)

        return workflow
