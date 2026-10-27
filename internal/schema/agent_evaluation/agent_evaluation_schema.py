"""测评 HTTP 请求和公开响应边界；评分领域约束见 agent_evaluation_entity。"""

from typing import Annotated, Literal
from uuid import UUID

from marshmallow import Schema, fields
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
)

from internal.entity.agent_execution_entity import AgentModelSelection
from internal.entity.agent_evaluation_entity import EvaluationEvidenceRule


class AgentEvaluationReq(BaseModel):
    """调用者只能选历史运行和已有模型连接，不能传入伪造任务结果。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    run_id: UUID
    request_id: UUID | None = None
    judge: AgentModelSelection
    mode: Literal["historical", "execute"] = "historical"
    max_iterations: StrictInt = Field(default=3, ge=1, le=5)
    evidence_rules: list[EvaluationEvidenceRule] = Field(
        default_factory=list, max_length=8
    )
    criteria: list[Annotated[StrictStr, Field(min_length=1, max_length=500)]] = Field(
        min_length=1, max_length=12
    )

    @field_validator("criteria")
    @classmethod
    def unique_criteria(cls, values: list[str]) -> list[str]:
        """重复标准会不合理地改变通过率，提交时直接拒绝。"""
        if len(set(values)) != len(values):
            raise ValueError("测评标准不能重复")
        return values


class EvaluationSourceResp(Schema):
    """列表轻量摘要；详情额外返回评分可见的原始文本。"""

    run_id = fields.UUID()
    conversation_id = fields.UUID()
    query = fields.String()
    answer = fields.String()
    status = fields.String()
    invoke_from = fields.String()
    created_at = fields.String(allow_none=True)
    finished_at = fields.String(allow_none=True)
    scope = fields.String()
    execution = fields.Dict()


class AgentEvaluationResp(Schema):
    """模型凭证和原始异常不得出现在测评记录中。"""

    id = fields.UUID()
    app_id = fields.UUID()
    run_id = fields.UUID(attribute="execution_id")
    source_snapshot = fields.Nested(EvaluationSourceResp)
    criteria = fields.List(fields.String())
    judge_config = fields.Dict()
    evaluator_version = fields.String()
    status = fields.String()
    result = fields.Dict(allow_none=True)
    error_message = fields.String()
    created_at = fields.DateTime()
    finished_at = fields.DateTime(allow_none=True)


class AgentEvaluationSummaryResp(AgentEvaluationResp):
    """历史列表不重复传输每轮候选及文件，完整证据由详情接口提供。"""

    result = fields.Method("summary_result")

    def summary_result(self, evaluation):
        """只裁剪运行证据，保留列表所需验收状态。"""
        result = evaluation.result
        if not result or "runtime" not in result:
            return result
        runtime = result["runtime"]
        return {
            "runtime": {
                key: runtime[key]
                for key in ("phase", "status", "accepted")
                if key in runtime
            }
            | {"rounds": []}
        }


class EvaluationPageResp(Schema):
    """统一页码元数据，客户端不用猜测是否还有结果。"""

    list = fields.List(fields.Nested(AgentEvaluationSummaryResp))
    total = fields.Integer()
    current_page = fields.Integer()
    page_size = fields.Integer()


class EvaluationSourcePageResp(EvaluationPageResp):
    """可选历史任务列表复用同一分页协议。"""

    list = fields.List(fields.Nested(EvaluationSourceResp))
