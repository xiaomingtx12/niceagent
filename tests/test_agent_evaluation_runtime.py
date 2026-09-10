"""通过真实 Deep Agents 图测试 Rubric 循环，仅替换付费模型响应。"""

from copy import deepcopy
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from internal.service.agent_evaluation_runtime import (
    execute_with_rubric,
    inspect_evidence,
    candidate_snapshot,
)


class ScriptedToolModel(FakeMessagesListChatModel):
    """保留框架工具路由及结构化输出，只固定模型返回的消息。"""

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        """模型桩接受框架生成的真实工具定义。"""
        return self


def tool_call(name, args):
    """产生真实 ToolCall 供 LangChain 路由到证据工具或输出 Schema。"""
    return AIMessage(
        content="",
        tool_calls=[
            {"name": name, "args": args, "id": str(uuid4()), "type": "tool_call"}
        ],
    )


def verdict(result="satisfied", passed=True):
    """构造符合原生裁判 Schema 的单标准结论。"""
    item = {"name": "C1", "passed": passed}
    if not passed:
        item["gap"] = "回答需要包含正确结论"
    return tool_call(
        "GraderResponse",
        {"result": result, "explanation": "已核验当前结果", "criteria": [item]},
    )


def run(answers, judgments, *, rules=None, max_iterations=3):
    """每次调用使用独立内存状态与检查点，模拟完整生成和评分链。"""
    progress = []
    result = execute_with_rubric(
        working_model=ScriptedToolModel(responses=answers),
        judge_model=ScriptedToolModel(responses=judgments),
        query="回答需要包含正确结论",
        criteria=["回答包含正确结论"],
        execution={
            "definition": {"preset_prompt": "按要求回答", "review_config": {}},
            "evidence_rules": rules or [],
            "max_iterations": max_iterations,
        },
        evaluation_id=str(uuid4()),
        on_progress=lambda value: progress.append(deepcopy(value)),
    )
    return result, progress


def test_real_graph_revises_using_fresh_evidence():
    """第一轮失败反馈通过原生中间件驱动第二轮修订，证据哈希同步变化。"""
    result, progress = run(
        [AIMessage(content="缺少结论"), AIMessage(content="正确结论")],
        [
            tool_call("inspect_candidate", {}),
            verdict("needs_revision", False),
            tool_call("inspect_candidate", {}),
            verdict(),
        ],
        rules=[{"kind": "contains", "value": "正确结论"}],
    )
    assert result["accepted"] is True
    assert result["status"] == "satisfied"
    assert len(result["rounds"]) == 2
    first, last = result["rounds"]
    assert first["candidate"]["answer"] == "缺少结论"
    assert first["evidence"]["ok"] is False
    assert last["evidence"]["ok"] is True
    assert first["candidate"]["candidate_hash"] != last["candidate"]["candidate_hash"]
    assert first["grading_run_id"] == last["grading_run_id"]
    assert any(item["phase"] == "revising" for item in progress)


def test_iteration_limit_is_not_acceptance():
    """0.6.12 回调仍为 needs_revision，业务终态要归为预算耗尽。"""
    result, _ = run(
        [AIMessage(content="错误")],
        [tool_call("inspect_candidate", {}), verdict("needs_revision", False)],
        max_iterations=1,
    )
    assert result["accepted"] is False
    assert result["status"] == "max_iterations_reached"
    assert result["rounds"][0]["verdict"] == "needs_revision"


@pytest.mark.parametrize(
    "judgments",
    [
        [verdict()],
        [
            tool_call("inspect_candidate", {}),
            tool_call(
                "GraderResponse",
                {"result": "satisfied", "explanation": "通过", "criteria": []},
            ),
        ],
        [tool_call("inspect_candidate", {}), verdict()],
    ],
)
def test_judge_cannot_bypass_evidence_or_coverage(judgments):
    """跳过取证、漏评及忽视确定性失败都不能放行候选。"""
    result, _ = run(
        [AIMessage(content="错误")],
        judgments,
        rules=[{"kind": "contains", "value": "正确结论"}],
    )
    assert result["accepted"] is False
    assert result["status"] == "grader_error"


def test_deterministic_rules_use_real_files_and_strict_json():
    """核验真实状态中的文件内容，拒绝用回答自述代替交付物。"""
    candidate = candidate_snapshot(
        {
            "messages": [AIMessage(content='{"answer": "ok"}')],
            "files": {"/report.md": {"content": ["hello"], "created_at": ""}},
        }
    )
    evidence = inspect_evidence(
        candidate,
        [
            {"kind": "json_keys", "value": "answer"},
            {"kind": "file_exists", "value": "/report.md"},
        ],
    )
    assert evidence["ok"] is True
    assert (
        inspect_evidence(candidate, [{"kind": "file_exists", "value": "/missing.md"}])[
            "ok"
        ]
        is False
    )


def test_progress_failure_stops_graph_instead_of_being_swallowed_by_callback():
    """业务持久化不放在原生观察回调内，保存失败必须终止后续修订。"""

    def unavailable(progress):
        """只让每轮评分结果写入失败，确认它不会被官方观察回调吞掉。"""
        if progress["phase"] == "finished":
            raise RuntimeError("persistence unavailable")

    with pytest.raises(RuntimeError, match="persistence unavailable"):
        execute_with_rubric(
            working_model=ScriptedToolModel(responses=[AIMessage(content="候选")]),
            judge_model=ScriptedToolModel(responses=[verdict()]),
            query="任务",
            criteria=["要求"],
            execution={
                "definition": {"preset_prompt": ""},
                "evidence_rules": [],
                "max_iterations": 1,
            },
            evaluation_id=str(uuid4()),
            on_progress=unavailable,
        )


def test_real_virtual_file_is_observed_by_grader():
    """工作模型写入 StateBackend 的文件必须实际出现在裁判证据中。"""
    result, _ = run(
        [
            tool_call(
                "write_file", {"file_path": "/report.md", "content": "本轮交付内容"}
            ),
            AIMessage(content="正确结论，文件已生成"),
        ],
        [tool_call("inspect_candidate", {}), verdict()],
        rules=[{"kind": "file_exists", "value": "/report.md"}],
    )
    assert result["accepted"] is True
    assert result["rounds"][0]["evidence"]["files"]["/report.md"] == "本轮交付内容"


def test_hidden_shell_tool_cannot_be_called():
    """StateBackend 隔离之外仍在工具边界阻止伪造命令调用。"""
    with pytest.raises(ValueError, match="不允许调用"):
        run([tool_call("execute", {"command": "echo forbidden"})], [verdict()])


def test_grader_exceptions_are_sanitized_and_not_accepted():
    """原生 grader_error 的原始异常不得进入持久化报告。"""
    from unittest.mock import patch
    from deepagents import RubricMiddleware

    with patch.object(
        RubricMiddleware, "_grade", side_effect=RuntimeError("secret-api-key")
    ):
        result, _ = run([AIMessage(content="正确结论")], [verdict()])
    assert result["accepted"] is False
    assert result["status"] == "grader_error"
    assert "secret-api-key" not in str(result)
