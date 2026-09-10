"""
腾讯云云函数（SCF）服务
用于安全地执行用户代码，避免直接在应用服务器上执行带来的安全风险
"""
import json
import time
from typing import Any, Dict

from tencentcloud.common import credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
    TencentCloudSDKException,
)
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile
from tencentcloud.scf.v20180416 import models, scf_client

from config import Config
from internal.exception import FailException


class TencentSCFService:
    """腾讯云云函数服务类"""

    def __init__(self):
        """初始化腾讯云SCF客户端"""
        config = Config()  # 实例化Config对象
        self.secret_id = config.TENCENT_SECRET_ID
        self.secret_key = config.TENCENT_SECRET_KEY
        self.region = config.TENCENT_SCF_REGION
        self.namespace = config.TENCENT_SCF_NAMESPACE
        self.function_name = config.TENCENT_SCF_FUNCTION_NAME

        # 创建认证对象
        cred = credential.Credential(self.secret_id, self.secret_key)

        # 实例化一个http选项
        httpProfile = HttpProfile()
        httpProfile.endpoint = "scf.tencentcloudapi.com"

        # 实例化一个client选项
        clientProfile = ClientProfile()
        clientProfile.httpProfile = httpProfile

        # 实例化要请求产品的client对象
        self.client = scf_client.ScfClient(cred, self.region, clientProfile)

    def invoke_function(
        self, code: str, params: Dict[str, Any], timeout: int = 30
    ) -> Dict[str, Any]:
        """
        调用云函数执行代码

        Args:
            code: 要执行的Python代码（必须包含main函数）
            params: 传递给main函数的参数字典
            timeout: 超时时间（秒）

        Returns:
            执行结果字典

        Raises:
            FailException: 执行失败时抛出
        """
        try:
            # 构建请求参数
            req = models.InvokeRequest()
            req.FunctionName = self.function_name
            req.Namespace = self.namespace
            req.InvocationType = "RequestResponse"  # 同步调用

            # 构建事件数据
            event_data = {"code": code, "params": params, "timeout": timeout}
            req.ClientContext = json.dumps(event_data)

            # 调用云函数
            start_time = time.time()
            resp = self.client.Invoke(req)
            elapsed_time = time.time() - start_time

            # 解析响应
            result = json.loads(resp.Result.RetMsg)

            # 检查执行状态
            if result.get("success"):
                return {
                    "success": True,
                    "data": result.get("data"),
                    "elapsed_time": elapsed_time,
                }
            else:
                error_msg = result.get("error", "未知错误")
                raise FailException(f"云函数执行失败: {error_msg}")

        except TencentCloudSDKException as e:
            raise FailException(f"调用腾讯云函数失败: {e.message}")
        except json.JSONDecodeError as e:
            raise FailException(f"解析云函数返回结果失败: {str(e)}")
        except Exception as e:
            raise FailException(f"执行代码时发生错误: {str(e)}")

    def check_function_exists(self) -> bool:
        """
        检查云函数是否存在

        Returns:
            bool: 函数存在返回True，否则返回False
        """
        try:
            req = models.GetFunctionRequest()
            req.FunctionName = self.function_name
            req.Namespace = self.namespace

            self.client.GetFunction(req)
            return True
        except TencentCloudSDKException:
            return False


# 全局单例
_scf_service_instance = None


def get_scf_service() -> TencentSCFService:
    """获取SCF服务单例"""
    global _scf_service_instance
    if _scf_service_instance is None:
        _scf_service_instance = TencentSCFService()
    return _scf_service_instance
