"""从实际运行状态提取候选并做确定性核验，不调用评分模型或访问数据库。"""

from copy import deepcopy
from hashlib import sha256
import json

from langchain_core.messages import AIMessage

from internal.core.agent_v2.execution.message_text import extract_text

MAX_CANDIDATE_CHARS = 48000
MAX_FILE_CHARS = 96000


def candidate_snapshot(state: dict) -> dict:
    """从框架实际状态提取当前答案和文件，裁判不能提交替代答案冒充取证。"""
    answer = ""
    for message in reversed(state.get("messages", [])):
        if isinstance(message, AIMessage) and not message.tool_calls:
            answer = extract_text(message.content)
            break
    if len(answer) > MAX_CANDIDATE_CHARS:
        raise ValueError("候选回答超过测评上限")
    files = {}
    for path, data in (state.get("files") or {}).items():
        if not isinstance(data, dict):
            continue
        content = data.get("content", "")
        if isinstance(content, list):
            content = "\n".join(content)
        if not isinstance(content, str):
            continue
        files[path] = content
    if len(files) > 50 or sum(len(value) for value in files.values()) > MAX_FILE_CHARS:
        raise ValueError("候选文件超过测评上限")
    payload = {"answer": answer, "files": files}
    payload["candidate_hash"] = sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    return payload


def inspect_evidence(candidate: dict, rules: list[dict]) -> dict:
    """对实际候选做只读核验，不执行候选代码，也不读取主机或生产文件。"""
    checks = []
    for index, rule in enumerate(rules, 1):
        kind, value = rule["kind"], rule["value"]
        if kind == "contains":
            passed = value in candidate["answer"]
            detail = "回答包含指定文本" if passed else "回答缺少指定文本"
        elif kind == "file_exists":
            passed = value in candidate["files"] and bool(
                candidate["files"][value].strip()
            )
            detail = "虚拟文件存在且非空" if passed else "虚拟文件不存在或为空"
        elif kind == "json_keys":
            try:
                parsed = json.loads(candidate["answer"])
                passed = isinstance(parsed, dict) and all(
                    key.strip() in parsed for key in value.split(",")
                )
            except (ValueError, TypeError):
                passed = False
            detail = (
                "JSON 对象包含全部要求字段"
                if passed
                else "回答不是有效 JSON 对象或缺少字段"
            )
        else:
            raise ValueError("不支持的证据核验类型")
        checks.append(
            {
                "id": f"E{index}",
                "kind": kind,
                "value": value,
                "passed": passed,
                "detail": detail,
            }
        )
    return {
        **deepcopy(candidate),
        "checks": checks,
        "ok": all(item["passed"] for item in checks),
    }
