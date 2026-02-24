import os

import yaml
from injector import inject, singleton
from pydantic import BaseModel, Field

from internal.core.builtin_workflows.entities.builtin_workflow_entity import (
    BuiltinWorkflowEntity,
)
from internal.core.builtin_workflows.entities.category_entity import (
    WorkflowCategoryEntity,
)


@inject
@singleton
class BuiltinWorkflowManager(BaseModel):
    """内置工作流模板管理器"""

    builtin_workflow_map: dict[str, BuiltinWorkflowEntity] = Field(default_factory=dict)
    categories: list[WorkflowCategoryEntity] = Field(default_factory=list)

    def __init__(self, **kwargs):
        """构造函数，初始化对应的builtin_workflow_map"""
        super().__init__(**kwargs)
        self._init_categories()
        self._init_builtin_workflow_map()

    def get_builtin_workflow(self, builtin_workflow_id: str) -> BuiltinWorkflowEntity:
        """根据传递的id获取内置工作流模板信息"""
        return self.builtin_workflow_map.get(builtin_workflow_id, None)

    def get_builtin_workflows(self) -> list[BuiltinWorkflowEntity]:
        """获取内置工作流模板实体列表信息"""
        return [
            builtin_workflow_entity
            for builtin_workflow_entity in self.builtin_workflow_map.values()
        ]

    def get_categories(self) -> list[WorkflowCategoryEntity]:
        """获取内置工作流模板分类列表信息"""
        return self.categories

    def _init_builtin_workflow_map(self):
        """内置工作流模板管理器初始化时初始化所有内置工作流模板信息"""
        # 1.检测builtin_workflow_map是否为空
        if self.builtin_workflow_map:
            return

        # 2.获取当前文件夹/类所在的文件夹路径
        current_path = os.path.abspath(__file__)
        parent_path = os.path.dirname(current_path)
        builtin_workflows_yaml_path = os.path.join(parent_path, "templates")

        # 3.循环遍历builtin_workflows_yaml_path读取底下的所有yaml文件
        for filename in os.listdir(builtin_workflows_yaml_path):
            if filename.endswith(".yaml") or filename.endswith(".yml"):
                file_path = os.path.join(builtin_workflows_yaml_path, filename)

                # 4.读取yaml数据
                with open(file_path, encoding="utf-8") as f:
                    builtin_workflow = yaml.safe_load(f)

                # 5.初始化内置工作流模板数据并添加到字典中
                self.builtin_workflow_map[builtin_workflow.get("id")] = (
                    BuiltinWorkflowEntity(**builtin_workflow)
                )

    def _init_categories(self):
        """初始化内置工作流模板分类列表信息"""
        # 1.检测数据是否已经处理
        if self.categories:
            return

        # 2.获取当前文件夹/类所在的文件夹路径
        current_path = os.path.abspath(__file__)
        parent_path = os.path.dirname(current_path)
        categories_yaml_path = os.path.join(
            parent_path, "categories", "categories.yaml"
        )

        # 3.读取yaml数据
        with open(categories_yaml_path, encoding="utf-8") as f:
            categories = yaml.safe_load(f)

        # 4.循环遍历所有分类数据并初始化
        for category in categories:
            self.categories.append(WorkflowCategoryEntity(**category))
