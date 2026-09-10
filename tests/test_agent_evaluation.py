"""真实 SQL 查询与模型解析链验证测评权限、证据和历史不可变性。"""

import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from pydantic import ValidationError
from sqlalchemy import JSON, MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from internal.exception import NotFoundException, ValidateErrorException
from internal.model import AgentRun, App, AppConfigVersion, Conversation, Message
from internal.model.agent_evaluation import AgentEvaluation
from internal.schema.agent_evaluation_schema import (
    AgentEvaluationReq,
    AgentEvaluationResp,
    EvaluationPageResp,
)
from internal.service.agent_evaluation_service import AgentEvaluationService
from internal.service.agent_evaluation_grader import evaluate_answer


def check(criterion_id=1, verdict="passed", quote="推荐方案 A", **kwargs):
    """模型桩仅替换第三方响应，结构验证使用生产解析器。"""
    return {
        "criterion_id": criterion_id,
        "verdict": verdict,
        "reason": "回答包含方案和理由",
        "score": {"passed": 1.0, "failed": 0.0, "insufficient_evidence": None}[verdict],
        "evidence_source": "answer" if quote else "none",
        "evidence_quote": quote,
        "suggestion": "",
        **kwargs,
    }


def model(*checks):
    """通过真实 LangChain 模型消息链产生 JSON。"""
    return FakeListChatModel(
        responses=[json.dumps({"checks": checks}, ensure_ascii=False)]
    )


@pytest.fixture
def environment():
    """仅在内存 SQLite 中建镜像表，不连接或迁移项目数据库。"""
    engine = create_engine("sqlite://")
    metadata = MetaData()
    for cls in (
        App,
        AppConfigVersion,
        Conversation,
        Message,
        AgentRun,
        AgentEvaluation,
    ):
        table = cls.__table__.to_metadata(metadata)
        table.indexes.clear()
        for column in table.columns:
            column.server_default = None
            if cls is not AgentEvaluation and not column.primary_key:
                column.nullable = True
            if isinstance(column.type, JSONB):
                column.type = JSON()
    metadata.create_all(engine)
    session = Session(engine)

    @contextmanager
    def auto_commit():
        """保留生产服务的事务边界，故障时回滚。"""
        try:
            yield
            session.commit()
        except Exception:
            session.rollback()
            raise

    owner, other, app_id = uuid4(), uuid4(), uuid4()
    session.add(App(id=app_id, account_id=owner))
    models = Mock()
    models.load_language_model.return_value = model(check())
    service = AgentEvaluationService(
        db=SimpleNamespace(session=session, auto_commit=auto_commit),
        language_model_service=models,
    )

    def add_run(
        account_id=owner,
        *,
        stable_owner=True,
        status="completed",
        source="creator_task",
        deleted=False
    ):
        """构造真实拥有者及来源组合，以数据库过滤验证授权边界。"""
        conversation_id, message_id, run_id = uuid4(), uuid4(), uuid4()
        conversation = Conversation(
            id=conversation_id,
            app_id=app_id,
            created_by=account_id,
            owner_account_id=account_id if stable_owner else None,
            is_deleted=deleted,
            invoke_from=source,
        )
        message = Message(
            id=message_id,
            app_id=app_id,
            conversation_id=conversation_id,
            query="请推荐方案并说明理由",
            answer="推荐方案 A，因为成本较低。",
            is_deleted=False,
        )
        run = AgentRun(
            id=run_id,
            app_id=app_id,
            conversation_id=conversation_id,
            message_id=message_id,
            status=status,
            invoke_from=source,
            created_at=datetime(2026, 9, 1),
            finished_at=datetime(2026, 9, 1),
        )
        session.add_all([conversation, message, run])
        session.commit()
        return run, message, conversation

    run, message, conversation = add_run()
    req = AgentEvaluationReq(
        run_id=run.id,
        judge={
            "provider": "test",
            "model": "judge-only",
            "provider_connection_id": str(uuid4()),
        },
        criteria=["回答给出方案与理由"],
    )
    with patch(
        "internal.service.agent_evaluation_service.run_agent_evaluation.apply_async"
    ) as dispatch:
        yield SimpleNamespace(
            dispatch=dispatch,
            service=service,
            session=session,
            owner=SimpleNamespace(id=owner),
            other=SimpleNamespace(id=other),
            app_id=app_id,
            run=run,
            message=message,
            conversation=conversation,
            req=req,
            add_run=add_run,
            models=models,
        )
    session.close()
    engine.dispose()


def complete(e):
    """经真实提交和后台服务完成评分，仅消息队列传输由测试替换。"""
    record = e.service.submit(e.app_id, e.owner, e.req)
    e.service.process(record.id)
    e.session.refresh(record)
    return record


def test_sources_and_direct_reads_enforce_identical_ownership(environment):
    """应用拥有者也不能通过测评入口查看别人的私有会话。"""
    e = environment
    hidden = [
        e.add_run(e.other.id),
        e.add_run(status="running"),
        e.add_run(source="web_app"),
        e.add_run(deleted=True),
    ]
    legacy = e.add_run(stable_owner=False)
    visible = e.service.list_sources(e.app_id, e.owner, 1, 10)
    assert {item["run_id"] for item in visible["list"]} == {
        str(e.run.id),
        str(legacy[0].id),
    }
    assert all("answer" not in item for item in visible["list"])
    for run, _, _ in hidden:
        with pytest.raises(NotFoundException):
            e.service.get_source(e.app_id, run.id, e.owner)
    with pytest.raises(NotFoundException):
        e.service.list_sources(e.app_id, e.other, 1, 10)
    e.conversation.owner_account_id = e.other.id
    e.session.commit()
    with pytest.raises(NotFoundException):
        e.service.get_source(e.app_id, e.run.id, e.owner)


def test_evaluation_freezes_material_and_uses_explicit_judge(environment):
    """两次评分各自追加，后续答案或应用变化不会重写原测评。"""
    e = environment
    evaluation = complete(e)
    e.models.load_language_model.assert_called_with(
        {**e.req.judge.model_dump(mode="json"), "parameters": {}}, e.owner.id
    )
    assert evaluation.result["outcome"] == "passed"
    assert evaluation.result["pass_rate"] == 100
    original_id = evaluation.id
    e.message.answer = "新的回答"
    e.session.commit()
    e.models.load_language_model.return_value = model(
        check(verdict="failed", quote="新的回答")
    )
    second = complete(e)
    assert second.id != original_id
    first = e.service.get_evaluation(e.app_id, original_id, e.owner)
    assert first.source_snapshot["answer"] == "推荐方案 A，因为成本较低。"
    assert e.run.status == "completed"
    assert e.message.answer == "新的回答"
    assert e.service.list_evaluations(e.app_id, e.owner, 1, 1)["total"] == 2
    with pytest.raises(NotFoundException):
        e.service.get_evaluation(e.app_id, original_id, e.other)
    # A removed source does not alter the saved evidence or silently point to a newer answer.
    e.conversation.is_deleted = True
    e.session.commit()
    assert (
        e.service.get_evaluation(e.app_id, original_id, e.owner).source_snapshot[
            "answer"
        ]
        == first.source_snapshot["answer"]
    )
    serialized = AgentEvaluationResp().dump(first)
    assert serialized["judge_config"]["model"] == "judge-only"
    page = EvaluationPageResp(exclude=("list.source_snapshot.answer",)).dump(
        {"list": [first]}
    )
    assert "answer" not in page["list"][0]["source_snapshot"]


@pytest.mark.parametrize(
    "checks",
    [
        [check(2)],
        [check(), check()],
        [check(quote="不存在的引用")],
        [check(quote="")],
        [check(quote="推荐方案 A", evidence_source="none")],
    ],
)
def test_invalid_judgments_are_saved_as_judge_errors(environment, checks):
    """漏评、重复或虚构引用都不是任务未通过，更不能伪装成评分成功。"""
    e = environment
    e.models.load_language_model.return_value = model(*checks)
    evaluation = complete(e)
    assert evaluation.status == "error"
    assert evaluation.result is None
    assert e.run.status == "completed"


def test_provider_failure_does_not_expose_exception_or_count_as_task_failure(
    environment,
):
    """第三方异常转换为固定用户文案，原始异常可能含连接凭证。"""
    e = environment
    llm = model(check())
    e.models.load_language_model.return_value = llm
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            FakeListChatModel,
            "_call",
            Mock(side_effect=RuntimeError("secret-provider-key")),
        )
        result = complete(e)
    assert result.status == "error"
    assert "secret" not in result.error_message
    assert result.result is None


def test_insufficient_evidence_has_no_false_percentage(environment):
    """对无法读取的交付物允许弃评，不能算作零分或满分。"""
    e = environment
    e.models.load_language_model.return_value = model(
        check(
            verdict="insufficient_evidence", quote="", reason="没有文件内容，无法核实"
        )
    )
    result = complete(e).result
    assert result["outcome"] == "insufficient_evidence"
    assert result["pass_rate"] is None
    assert result["insufficient"] == 1


def test_large_material_is_rejected_without_silent_truncation_or_model_call(
    environment,
):
    """评分只能基于用户看到的完整材料，长度超限时不消费模型。"""
    e = environment
    e.message.answer = "答" * 48001
    e.session.commit()
    with pytest.raises(ValidateErrorException):
        complete(e)
    e.models.load_language_model.assert_not_called()


@pytest.mark.parametrize(
    "override",
    [
        {"criteria": []},
        {"criteria": [" "]},
        {"criteria": ["相同", "相同"]},
        {"criteria": ["a" * 501]},
        {"answer": "客户端伪造答案"},
        {
            "judge": {
                "provider": "p",
                "model": "m",
                "provider_connection_id": str(uuid4()),
                "api_key": "secret",
            }
        },
    ],
)
def test_request_rejects_invalid_standards_and_untrusted_execution_fields(
    environment, override
):
    """嵌套请求执行严格校验，客户端无权改变评分材料或传入连接凭证。"""
    with pytest.raises(ValidationError):
        AgentEvaluationReq.model_validate({**environment.req.model_dump(), **override})


@pytest.mark.parametrize(
    "verdict,score",
    [
        ("passed", 0.0),
        ("passed", 0.5),
        ("failed", 1.0),
        ("failed", 0.3),
        ("insufficient_evidence", 0.0),
        ("passed", None),
    ],
)
def test_inconsistent_scores_are_judge_errors(environment, verdict, score):
    """真实解析器拒绝结论与分数矛盾，不能成为对用户可见的成绩。"""
    e = environment
    e.models.load_language_model.return_value = model(
        check(verdict=verdict, score=score)
    )
    assert complete(e).status == "error"


def test_missing_score_is_not_silently_zero(environment):
    """缺失必填分数属于评分故障。"""
    e = environment
    payload = check()
    payload.pop("score")
    e.models.load_language_model.return_value = model(payload)
    assert complete(e).result is None


def test_partial_evidence_has_explicit_denominator(environment):
    """部分可评时分别统计覆盖率和已评项得分，整体成绩保持空。"""
    e = environment
    e.req = e.req.model_copy(update={"criteria": ["结论", "解释", "文件核验"]})
    e.models.load_language_model.return_value = model(
        check(1),
        check(2, verdict="failed", score=0.5),
        check(3, verdict="insufficient_evidence", quote=""),
    )
    result = complete(e).result
    assert result["score"] is None
    assert result["evaluated_score"] == 0.75
    assert result["evaluated"] == 2
    assert result["coverage"] == 66.7
    assert result["evaluated_pass_rate"] == 50
    assert result["outcome"] == "failed"
    assert result["checks"][2]["score"] is None
    assert "confidence" not in result


def test_submission_and_delivery_are_idempotent(environment):
    """HTTP 重试复用记录，重复队列消息不会再次调用评分链。"""
    e = environment
    req = e.req.model_copy(update={"request_id": uuid4()})
    with patch(
        "internal.service.agent_evaluation_service.evaluate_answer",
        wraps=evaluate_answer,
    ) as grade:
        record = e.service.submit(e.app_id, e.owner, req)
        assert record.status == "queued"
        assert record.finished_at is None
        assert grade.call_count == 0
        assert e.service.submit(e.app_id, e.owner, req).id == record.id
        e.dispatch.assert_called_once()
        e.message.answer = "后来被修改的回答"
        e.session.commit()
        e.service.process(record.id)
        e.service.process(record.id)
        e.session.refresh(record)
        assert grade.call_count == 1
        assert record.status == "completed"
        assert record.source_snapshot["answer"] == "推荐方案 A，因为成本较低。"
        assert e.service.submit(e.app_id, e.owner, req).id == record.id
    with pytest.raises(ValidateErrorException):
        e.service.submit(
            e.app_id, e.owner, req.model_copy(update={"criteria": ["不同要求"]})
        )
    assert (
        e.service.submit(
            e.app_id, e.owner, req.model_copy(update={"request_id": uuid4()})
        ).id
        != record.id
    )


def test_dispatch_failure_and_stale_work_are_not_agent_failures(environment):
    """队列不可用与进程失联都保存故障，不让记录永久处于进行中。"""
    e = environment
    e.dispatch.side_effect = RuntimeError("secret-broker-url")
    record = e.service.submit(e.app_id, e.owner, e.req)
    assert record.status == "error"
    assert "secret" not in record.error_message
    e.dispatch.side_effect = None
    record = e.service.submit(e.app_id, e.owner, e.req)
    record.created_at = e.service._now() - timedelta(minutes=16)
    e.session.commit()
    e.service.process(record.id)
    e.session.refresh(record)
    assert record.status == "error"
    assert record.result is None
    e.service._finish(record.id, ("running",), result={"outcome": "passed"})
    e.session.refresh(record)
    assert record.status == "error"


def test_worker_rechecks_app_ownership(environment):
    """排队后应用归属变化，后台不能继续使用原账号模型执行。"""
    e = environment
    record = e.service.submit(e.app_id, e.owner, e.req)
    e.session.get(App, e.app_id).account_id = e.other.id
    e.session.commit()
    e.models.reset_mock()
    e.service.process(record.id)
    e.session.refresh(record)
    assert record.status == "error"
    e.models.load_language_model.assert_not_called()


def test_unmigrated_finished_at_has_safe_actionable_error(environment):
    """旧库的非空约束必须转成业务错误，不泄露 SQL、回答或连接信息。"""
    from sqlalchemy.exc import IntegrityError
    from internal.exception import FailException

    class LegacyConstraintError(Exception):
        """复现日志中的 PostgreSQL 诊断字段，不依赖生产数据库。"""

        pgcode = "23502"
        diag = SimpleNamespace(table_name="agent_evaluation", column_name="finished_at")

    e = environment
    error = IntegrityError(
        "INSERT secret-query", {"answer": "private-answer"}, LegacyConstraintError()
    )
    with patch.object(AgentEvaluationService, "create", side_effect=error):
        with pytest.raises(FailException, match="数据库结构尚未更新") as caught:
            e.service.submit(e.app_id, e.owner, e.req)
    assert "private-answer" not in str(caught.value)
    assert "secret" not in str(caught.value)
    e.dispatch.assert_not_called()


def test_worker_does_not_expire_unrelated_evaluations(environment):
    """单个 Worker 只负责自己的记录，不应顺带更新全库过期任务。"""
    e = environment
    unrelated = e.service.submit(e.app_id, e.owner, e.req)
    unrelated.created_at = e.service._now() - timedelta(minutes=16)
    e.session.commit()
    current = e.service.submit(e.app_id, e.owner, e.req)
    e.service.process(current.id)
    e.session.refresh(unrelated)
    assert unrelated.status == "queued"
    assert e.service.get_evaluation(e.app_id, unrelated.id, e.owner).status == "error"


def test_unrelated_integrity_error_is_not_reported_as_migration(environment):
    """不能把其他列的约束错误或程序缺陷误判为旧版本数据库。"""
    from sqlalchemy.exc import IntegrityError

    class OtherConstraintError(Exception):
        """保留真实 SQLSTATE，但指向不同列。"""

        pgcode = "23502"
        diag = SimpleNamespace(table_name="agent_evaluation", column_name="account_id")

    e = environment
    failure = IntegrityError("INSERT", {}, OtherConstraintError())
    with patch.object(AgentEvaluationService, "create", side_effect=failure):
        with pytest.raises(IntegrityError) as caught:
            e.service.submit(e.app_id, e.owner, e.req)
    assert caught.value is failure
    e.dispatch.assert_not_called()


def test_unique_conflict_reuses_the_winning_submission(environment):
    """并发提交已由另一事务落库时，失败方读取胜出记录且不重复入队。"""
    from sqlalchemy.exc import IntegrityError

    class DuplicateError(Exception):
        """模拟 PostgreSQL 唯一键竞争的驱动诊断。"""

        pgcode = "23505"

    e = environment
    req = e.req.model_copy(update={"request_id": uuid4()})
    create_record = e.service.create

    def competing_insert(cls, **values):
        """将胜出事务写入数据库，再向当前提交抛出唯一键冲突。"""
        create_record(cls, **values)
        raise IntegrityError("INSERT", {}, DuplicateError())

    with patch.object(AgentEvaluationService, "create", side_effect=competing_insert):
        record = e.service.submit(e.app_id, e.owner, req)
    assert record.status == "queued"
    assert e.session.query(AgentEvaluation).count() == 1
    e.dispatch.assert_not_called()


def test_database_outage_is_not_misreported_as_judge_failure(environment):
    """数据库异常留给基础设施处理，不能污染评分失败统计。"""
    from sqlalchemy.exc import OperationalError

    e = environment
    record = e.service.submit(e.app_id, e.owner, e.req)
    failure = OperationalError("SELECT", {}, RuntimeError("database unavailable"))
    e.models.load_language_model.side_effect = failure
    with pytest.raises(OperationalError):
        e.service.process(record.id)
    e.session.refresh(record)
    assert record.status == "running"
    assert record.result is None
    assert record.error_message == ""


def test_execution_mode_freezes_definition_and_keeps_original_task(environment):
    """排队时冻结模型和提示词，后续修改草稿不影响本次验收，也不改原回答。"""
    from internal.entity.app_entity import AppConfigType

    e = environment
    original = e.message.answer
    config = {**e.req.judge.model_dump(mode="json"), "parameters": {}}
    draft = AppConfigVersion(
        id=uuid4(),
        app_id=e.app_id,
        config_type=AppConfigType.DRAFT,
        model_config=config,
        preset_prompt="冻结提示词",
        review_config={},
    )
    e.session.add(draft)
    e.session.commit()
    req = e.req.model_copy(update={"mode": "execute", "request_id": uuid4()})
    record = e.service.submit(e.app_id, e.owner, req)
    assert (
        record.source_snapshot["execution"]["definition"]["preset_prompt"]
        == "冻结提示词"
    )
    draft.preset_prompt = "新提示词"
    e.session.commit()
    runtime = {
        "phase": "finished",
        "status": "satisfied",
        "accepted": True,
        "rounds": [],
        "final_answer": "新候选",
    }
    with patch(
        "internal.service.agent_evaluation_service.execute_with_rubric",
        return_value=runtime,
    ) as runner:
        e.service.process(record.id)
    assert (
        runner.call_args.kwargs["execution"]["definition"]["preset_prompt"]
        == "冻结提示词"
    )
    assert runner.call_args.kwargs["query"] == e.message.query
    assert e.message.answer == original
    assert e.run.status == "completed"
    e.session.refresh(record)
    assert record.result["runtime"]["accepted"] is True
    serialized = AgentEvaluationResp().dump(record)
    assert serialized["source_snapshot"]["execution"]["mode"] == "execute"
    with pytest.raises(ValidateErrorException):
        e.service.submit(
            e.app_id, e.owner, req.model_copy(update={"mode": "historical"})
        )
    with pytest.raises(ValidateErrorException):
        e.service.submit(
            e.app_id, e.owner, req.model_copy(update={"max_iterations": 5})
        )


def test_runtime_error_preserves_rounds_but_never_accepts(environment):
    """后续异常保留已保存的轮次，记录状态仍为 error。"""
    e = environment
    record = e.service.submit(e.app_id, e.owner, e.req)
    record.status = "running"
    e.session.commit()
    e.service._save_runtime_progress(
        record.id,
        {"phase": "revising", "accepted": False, "rounds": [{"iteration": 0}]},
    )
    e.service._finish(record.id, ("running",), error="执行失败")
    e.session.refresh(record)
    assert record.status == "error"
    assert record.result["runtime"]["rounds"] == [{"iteration": 0}]
    assert record.result["runtime"]["accepted"] is False
    page = EvaluationPageResp().dump({"list": [record]})
    assert page["list"][0]["result"]["runtime"]["rounds"] == []


@pytest.mark.parametrize(
    "override",
    [
        {"max_iterations": 0},
        {"max_iterations": 6},
        {"max_iterations": True},
        {"mode": "unknown"},
        {"evidence_rules": [{"kind": "file_exists", "value": "/../secret"}]},
        {"evidence_rules": [{"kind": "json_keys", "value": ","}]},
        {"evidence_rules": [{"kind": "execute", "value": "arbitrary command"}]},
    ],
)
def test_runtime_input_rejects_unbounded_or_host_operations(environment, override):
    """外部请求只可选择受限核验类型，不能注入命令或扩大运行预算。"""
    with pytest.raises(ValidationError):
        AgentEvaluationReq.model_validate({**environment.req.model_dump(), **override})
