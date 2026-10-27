"""测评规则和裁判输出约束，独立于 HTTP 请求、数据库及 Agent 框架。"""

from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)


class EvaluationEvidenceRule(BaseModel):
    """核验只支持文本、JSON 顶层字段和虚拟文件，不接受任意代码。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["contains", "json_keys", "file_exists"]
    value: StrictStr = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def valid_target(self) -> "EvaluationEvidenceRule":
        """提交请求和读取冻结规则共用约束，文件路径不能逃出虚拟空间。"""
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


class CriterionGrade(BaseModel):
    """评分必须遵守固定分档，缺少证据时允许弃评。"""

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
    def consistent_score(self) -> "CriterionGrade":
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
    """裁判输出还需与本次材料核对，结构合法不代表证据真实。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    checks: list[CriterionGrade] = Field(min_length=1, max_length=12)

    def validate_material(
        self, *, query: str, answer: str, criterion_count: int
    ) -> None:
        """禁止漏评、重复评分及引用不存在的原文。"""
        ids = sorted(check.criterion_id for check in self.checks)
        if ids != list(range(1, criterion_count + 1)):
            raise ValueError("评分未逐项覆盖标准")
        material = {"query": query, "answer": answer, "none": ""}
        for check in self.checks:
            quote = check.evidence_quote
            if quote and quote not in material[check.evidence_source]:
                raise ValueError("评分引用不在原始文本中")
            if check.verdict == "passed" and not quote:
                raise ValueError("通过项缺少证据")
