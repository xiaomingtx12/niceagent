"""
基于腾讯云云函数的安全代码执行节点
所有代码在隔离的云函数环境中执行，确保系统安全
"""
import ast
import time
from typing import Optional

from langchain_core.runnables import RunnableConfig

from internal.core.workflow.entities.node_entity import NodeResult, NodeStatus
from internal.core.workflow.entities.variable_entity import (
    VARIABLE_TYPE_DEFAULT_VALUE_MAP,
)
from internal.core.workflow.entities.workflow_entity import WorkflowState
from internal.core.workflow.nodes import BaseNode
from internal.core.workflow.utils.helper import extract_variables_from_state
from internal.exception import FailException
from internal.service.workflow.tencent_scf_service import get_scf_service

from .code_entity import CodeNodeData


class CodeNode(BaseNode):
    """基于腾讯云云函数的Python代码运行节点（安全版本）"""

    node_data: CodeNodeData

    def invoke(
        self, state: WorkflowState, config: Optional[RunnableConfig] = None
    ) -> WorkflowState:
        """
        使用腾讯云云函数执行Python代码
        代码在隔离的云函数环境中运行，避免安全风险
        """
        # 1.从状态中提取输入数据
        start_at = time.perf_counter()
        inputs_dict = extract_variables_from_state(self.node_data.inputs, state)

        # 2.验证代码格式（本地快速校验）
        self._validate_code_format(self.node_data.code)

        # 3.通过腾讯云云函数执行代码（安全隔离）
        scf_service = get_scf_service()
        scf_result = scf_service.invoke_function(
            code=self.node_data.code, params=inputs_dict, timeout=30
        )

        result = scf_result.get("data")

        # 4.检测函数的返回值是否为字典
        if not isinstance(result, dict):
            raise FailException("main函数的返回值必须是一个字典")

        # 5.提取输出数据
        outputs_dict = {}
        outputs = self.node_data.outputs
        for output in outputs:
            # 6.提取输出数据(非严格校验)
            outputs_dict[output.name] = result.get(
                output.name,
                VARIABLE_TYPE_DEFAULT_VALUE_MAP.get(output.type),
            )

        # 7.构建状态数据并返回
        return {
            "node_results": [
                NodeResult(
                    node_data=self.node_data,
                    status=NodeStatus.SUCCEEDED,
                    inputs=inputs_dict,
                    outputs=outputs_dict,
                    latency=(time.perf_counter() - start_at),
                )
            ]
        }

    @classmethod
    def _validate_code_format(cls, code: str):
        """
        本地验证代码格式（快速校验，避免无效请求）
        实际的安全执行由云函数沙箱保证
        """
        try:
            # 1.解析代码为AST(抽象语法树)
            tree = ast.parse(code)

            # 2.定义变量用于检查是否找到main函数
            main_func = None

            # 3.循环遍历语法树
            for node in tree.body:
                # 4.判断节点类型是否为函数
                if isinstance(node, ast.FunctionDef):
                    # 5.检查函数名称是否为main
                    if node.name == "main":
                        if main_func:
                            raise FailException("代码中只能有一个main函数")

                        # 6.检测main函数的参数是否为params
                        if (
                            len(node.args.args) != 1
                            or node.args.args[0].arg != "params"
                        ):
                            raise FailException(
                                "main函数必须只有一个参数，且参数为params"
                            )

                        main_func = node
                    else:
                        # 7.其他函数的情况，直接抛出错误
                        raise FailException("代码中不能包含其他函数，只能有main函数")
                else:
                    # 8.非函数的情况，直接抛出错误
                    raise FailException("代码中只能包含函数定义，不允许其他语句存在")

            # 9.判断下是否找到main函数
            if not main_func:
                raise FailException("代码中必须包含名为main的函数")

        except SyntaxError as e:
            raise FailException(f"代码语法错误: {str(e)}")
        except Exception as e:
            if isinstance(e, FailException):
                raise
            raise FailException(f"代码验证失败: {str(e)}")
