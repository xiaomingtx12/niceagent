"""测评请求、评分模型输出和公开响应边界。"""

from typing import Annotated, Literal
from uuid import UUID

from marshmallow import Schema, fields
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from internal.entity.agent_workbench_entity import WorkbenchModelSelection


class EvaluationEvidenceRuleReq(BaseModel):
    """限定确定性核验类型；不接受任意代码、命令或主机路径。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["contains", "json_keys", "file_exists"]
    value: StrictStr = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def valid_target(self):
        """文件核验只允许本次虚拟文件空间，JSON 只检查顶层字段。"""
        if self.kind == "file_exists" and (
            not self.value.startswith("/")
            or ".." in self.value.split("/")
            or "\\" in self.value
        ):
            raise ValueError("请使用虚拟空间中的绝对路径，如 /report.md")
        if self.kind == "json_keys" and not all(
            part.strip() for part in self.value.split(",")
        ):
            raise ValueError("JSON 字段名不能为空")
        return self


class AgentEvaluationReq(BaseModel):
    """调用者只能选历史运行和已有模型连接，不能传入伪造任务结果。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    run_id: UUID
    request_id: UUID | None = None
    judge: WorkbenchModelSelection
    mode: Literal["historical", "execute"] = "historical"
    max_iterations: StrictInt = Field(default=3, ge=1, le=5)
    evidence_rules: list[EvaluationEvidenceRuleReq] = Field(
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


class CriterionGrade(BaseModel):
    """证据必须引用提供的文本；缺少材料时允许明确弃评。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    criterion_id: StrictInt = Field(ge=1, le=12)
    verdict: Literal["passed", "failed", "insufficient_evidence"]
    reason: StrictStr = Field(min_length=1, max_length=1500)
    evidence_source: Literal["query", "answer", "none"]
    evidence_quote: StrictStr = Field(max_length=1500)
    suggestion: StrictStr = Field(max_length=1500)
    score: StrictFloat | None = Field(
        description="完全满足 1.0，部分满足 0.5，不满足 0.0，证据不足 null"
    )

    @model_validator(mode="after")
    def consistent_score(self):
        """分档与结论必须一致，缺失证据不能伪装成零分。"""
        allowed = {
            "passed": (1.0,),
            "failed": (0.0, 0.5),
            "insufficient_evidence": (None,),
        }
        if self.score not in allowed[self.verdict]:
            raise ValueError("评分档位与结论不一致")
        return self


class EvaluationGrade(BaseModel):
    """一次独立评分的结构化输出，不包含执行或重试指令。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    checks: list[CriterionGrade] = Field(min_length=1, max_length=12)


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
    run_id = fields.UUID()
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
