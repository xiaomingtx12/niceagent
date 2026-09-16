"""提取本轮实际交付物并核验；不调用模型、不读取主机文件。"""

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import TypedDict

from langchain_core.messages import AIMessage, HumanMessage

from internal.core.agent_v2.execution.message_text import extract_text
from internal.entity.agent_evaluation_entity import EvaluationEvidenceRule

MAX_CANDIDATE_CHARS = 48000
MAX_FILE_CHARS = 96000
MAX_CANDIDATE_FILES = 50


@dataclass
class EvaluationCandidate:
    """候选与图状态分离；哈希只包含答案和文件，不受评分或文件时间戳影响。"""

    answer: str
    files: dict[str, str]
    candidate_hash: str

    def to_dict(self) -> dict:
        """工具和持久化得到独立容器，不能通过返回值修改本轮证据。"""
        return {
            "answer": self.answer,
            "files": dict(self.files),
            "candidate_hash": self.candidate_hash,
        }


class EvidenceCheck(TypedDict):
    """单条程序核验结果；E 编号与裁判的 C 标准编号相互独立。"""

    id: str
    kind: str
    value: str
    passed: bool
    detail: str


@dataclass
class EvaluationEvidence:
    """核验结果始终绑定原候选，裁判不能传入另一份答案接受检查。"""

    candidate: EvaluationCandidate
    checks: list[EvidenceCheck]

    @property
    def ok(self) -> bool:
        """没有额外规则只表示程序检查通过，语义标准仍由裁判验收。"""
        return all(check["passed"] for check in self.checks)

    def to_dict(self) -> dict:
        """仅在工具返回和记录保存边界展开证据，内部不重复复制候选文件。"""
        return {
            **self.candidate.to_dict(),
            "checks": [dict(check) for check in self.checks],
            "ok": self.ok,
        }


def candidate_snapshot(state: dict) -> EvaluationCandidate:
    """答案限定在本轮，文件限定在 StateBackend；超限拒绝评分，不截断取证。"""
    answer = _current_answer(state.get("messages", []))
    if len(answer) > MAX_CANDIDATE_CHARS:
        raise ValueError("候选回答超过测评上限")
    files = _candidate_files(state.get("files", {}))
    payload = json.dumps(
        {"answer": answer, "files": files}, sort_keys=True, ensure_ascii=False
    )
    return EvaluationCandidate(
        answer=answer,
        files=files,
        candidate_hash=sha256(payload.encode("utf-8")).hexdigest(),
    )


def inspect_evidence(
    candidate: EvaluationCandidate,
    rules: list[EvaluationEvidenceRule],
) -> EvaluationEvidence:
    """程序检查和语义评分分开；同一回答的 JSON 最多解析一次。"""
    json_keys = (
        _answer_json_keys(candidate.answer)
        if any(rule.kind == "json_keys" for rule in rules)
        else None
    )
    checks: list[EvidenceCheck] = []
    for index, rule in enumerate(rules, 1):
        if rule.kind == "contains":
            passed = rule.value in candidate.answer
            detail = "回答包含指定文本" if passed else "回答缺少指定文本"
        elif rule.kind == "file_exists":
            passed = bool(candidate.files.get(rule.value, "").strip())
            detail = "虚拟文件存在且非空" if passed else "虚拟文件不存在或为空"
        elif rule.kind == "json_keys":
            required_keys = {key.strip() for key in rule.value.split(",")}
            passed = json_keys is not None and required_keys <= json_keys
            detail = (
                "JSON 对象包含全部要求字段"
                if passed
                else "回答不是有效 JSON 对象或缺少字段"
            )
        else:
            raise ValueError("不支持的证据核验类型")
        checks.append(
            EvidenceCheck(
                id=f"E{index}",
                kind=rule.kind,
                value=rule.value,
                passed=passed,
                detail=detail,
            )
        )
    return EvaluationEvidence(candidate=candidate, checks=checks)


def _current_answer(messages: list) -> str:
    """遇到当前提问或尚未完成的工具调用就停止，不能退回上一轮答案。"""
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return ""
        if isinstance(message, AIMessage):
            return "" if message.tool_calls else extract_text(message.content)
    return ""


def _candidate_files(raw_files: dict) -> dict[str, str]:
    """执行图固定使用 v2 文本文件；无效内容报错，不能静默漏掉待验收文件。"""
    if not isinstance(raw_files, dict):
        raise ValueError("候选文件状态格式无效")
    if len(raw_files) > MAX_CANDIDATE_FILES:
        raise ValueError("候选文件数量超过测评上限")
    files: dict[str, str] = {}
    total_chars = 0
    for path, data in raw_files.items():
        if not isinstance(path, str) or not isinstance(data, dict):
            raise ValueError("候选文件状态格式无效")
        content = data.get("content")
        if not isinstance(content, str) or data.get("encoding") != "utf-8":
            raise ValueError("候选文件必须为 v2 UTF-8 文本")
        total_chars += len(content)
        if total_chars > MAX_FILE_CHARS:
            raise ValueError("候选文件内容超过测评上限")
        files[path] = content
    return files


def _answer_json_keys(answer: str) -> set[str] | None:
    """只接受完整 JSON 对象，JSON 无效属于核验未通过，不是服务故障。"""
    try:
        parsed = json.loads(answer)
    except json.JSONDecodeError:
        return None
    return set(parsed) if isinstance(parsed, dict) else None
