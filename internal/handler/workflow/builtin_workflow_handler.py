from dataclasses import dataclass

from flask_login import login_required, current_user
from injector import inject

from internal.schema.workflow.builtin_workflow_schema import (
    GetBuiltinWorkflowCategoriesResp,
    GetBuiltinWorkflowsResp,
    GetBuiltinWorkflowDetailResp,
    CreateWorkflowFromTemplateReq,
)
from internal.service import BuiltinWorkflowService
from pkg.response import success_json, validate_error_json


@inject
@dataclass
class BuiltinWorkflowHandler:
    """内置工作流模板处理器"""

    builtin_workflow_service: BuiltinWorkflowService

    @login_required
    def get_builtin_workflow_categories(self):
        """获取内置工作流模板分类列表信息"""
        categories = self.builtin_workflow_service.get_categories()
        resp = GetBuiltinWorkflowCategoriesResp(many=True)
        return success_json(resp.dump(categories))

    @login_required
    def get_builtin_workflows(self):
        """获取所有内置工作流模板列表信息"""
        builtin_workflows = self.builtin_workflow_service.get_builtin_workflows()
        resp = GetBuiltinWorkflowsResp(many=True)
        return success_json(resp.dump(builtin_workflows))

    @login_required
    def get_builtin_workflow_detail(self, builtin_workflow_id: str):
        """获取内置工作流模板详情"""
        builtin_workflow = self.builtin_workflow_service.get_builtin_workflow_detail(
            builtin_workflow_id
        )
        resp = GetBuiltinWorkflowDetailResp()
        return success_json(resp.dump(builtin_workflow))

    @login_required
    def create_workflow_from_template(self, builtin_workflow_id: str):
        """从模板创建工作流实例"""
        # 1.提取请求并校验
        req = CreateWorkflowFromTemplateReq()
        if not req.validate():
            return validate_error_json(req.errors)

        # 2.从模板创建工作流实例
        workflow = self.builtin_workflow_service.create_workflow_from_template(
            builtin_workflow_id, req.name.data, current_user
        )

        return success_json({"id": str(workflow.id)})
