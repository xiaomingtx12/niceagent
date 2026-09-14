"""将冻结的测评配置交给独立图执行，并汇总 Core 交付的验收记录。"""

import json
import time
from collections.abc import Callable

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain.agents.middleware import (
    ModelCallLimitMiddleware,
    ToolCallLimitMiddleware,
)
from langchain_core.messages import HumanMessage
from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.memory import InMemorySaver

from internal.core.agent_v2.execution.evaluation_evidence import candidate_snapshot
from internal.core.agent_v2.middleware.evaluation_tool_middleware import EvaluationToolBoundary
from internal.core.agent_v2.middleware.evidence_rubric_middleware import EvidenceRubricMiddleware

EXECUTION_VERSION = "rubric-runtime-v1"
EXECUTION_SCOPE = "使用提交时 Agent 的模型和提示词，在独立会话与临时文件空间运行；不接入生产工具、知识库、Skills、子 Agent、长期记忆或历史附件。"


def execute_with_rubric(
    *,
    working_model: BaseChatModel,
    judge_model: BaseChatModel,
    query: str,
    criteria: list[str],
    execution: dict,
    evaluation_id: str,
    on_progress: Callable[[dict], None],
) -> dict:
    """从冻结配置创建一次性图；最后验收由应用确认，不因有回答而放行。"""
    started_at = time.monotonic()
    middleware = EvidenceRubricMiddleware(
        model=judge_model,
        criteria=criteria,
        rules=execution["evidence_rules"],
        max_iterations=execution["max_iterations"],
        on_progress=on_progress,
    )
    rubric = "\n".join(f"C{i}: {text}" for i, text in enumerate(criteria, 1))
    rubric += (
        "\n必须调用 inspect_candidate，checks 全部通过且 C 标准均满足后才可 satisfied。"
    )
    definition = execution["definition"]
    graph = create_deep_agent(
        model=working_model,
        system_prompt=definition["preset_prompt"]
        + "\n"
        + EXECUTION_SCOPE
        + "\n根据验收差距修订当前结果。需要文件时使用临时文件空间；最终回答必须说明交付结果。",
        backend=StateBackend(),
        checkpointer=InMemorySaver(),
        middleware=[
            EvaluationToolBoundary(),
            ModelCallLimitMiddleware(run_limit=30, exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=50, exit_behavior="error"),
            middleware,
        ],
    )
    instructions = query + "\n\n本次验收要求：\n" + rubric
    if execution["evidence_rules"]:
        instructions += "\n确定性核验要求：" + json.dumps(
            execution["evidence_rules"], ensure_ascii=False
        )
    on_progress({"phase": "generating", "rounds": [], "accepted": False})
    result = graph.invoke(
        {"messages": [HumanMessage(content=instructions)], "rubric": rubric},
        config={"configurable": {"thread_id": evaluation_id}, "recursion_limit": 150},
    )
    return _execution_result(
        result,
        rounds=middleware.rounds,
        max_iterations=execution["max_iterations"],
        started_at=started_at,
    )


def _execution_result(
    state: dict, *, rounds: list[dict], max_iterations: int, started_at: float,
) -> dict:
    """只有末轮通过且候选哈希仍与最终状态一致才放行，图正常结束不代表验收通过。"""
    current = candidate_snapshot(state)
    final = rounds[-1] if rounds else None
    accepted = bool(
        final
        and final["status"] == "satisfied"
        and final["candidate"]["candidate_hash"] == current["candidate_hash"]
        and final["evidence"]
    )
    status = final["status"] if final else "grader_error"
    if status == "satisfied" and not accepted:
        status = "grader_error"
    return {
        "duration_ms": round((time.monotonic() - started_at) * 1000),
        "first_pass": bool(rounds and rounds[0]["status"] == "satisfied"),
        "phase": "finished",
        "status": status,
        "accepted": accepted,
        "rounds": rounds,
        "final_answer": current["answer"],
        "files": current["files"],
        "max_iterations": max_iterations,
        "scope": EXECUTION_SCOPE,
    }
