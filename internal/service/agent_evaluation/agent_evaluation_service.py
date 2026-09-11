"""对冻结的任务回答做独立测评，不介入生产 Agent 图。"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from functools import partial
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from injector import inject
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Query

from internal.exception import FailException, NotFoundException, ValidateErrorException
from internal.extension.logging_extension import logger
from internal.model import (
    Account,
    AgentRun,
    App,
    AppConfigVersion,
    Conversation,
    Message,
)
from internal.entity.app_entity import AppConfigType
from internal.model.agent_evaluation import AgentEvaluation
from internal.schema.agent_evaluation.agent_evaluation_schema import AgentEvaluationReq
from internal.schema.app.app_config_schema import AppModelConfigReq
from internal.task.agent_evaluation_task import run_agent_evaluation
from pkg.sqlalchemy import SQLAlchemy
from .agent_evaluation_grader import EVALUATOR_VERSION, evaluate_answer
from internal.service.common.base_service import BaseService
from internal.service.language_model.language_model_service import LanguageModelService
from .agent_evaluation_runtime import (
    EXECUTION_VERSION,
    EXECUTION_SCOPE,
    execute_with_rubric,
)

EVALUATION_DEADLINE_SECONDS = 900
EVIDENCE_SCOPE = (
    "仅本轮任务描述与最终回答；不包含此前对话、附件内容、工具轨迹及外部系统核验。"
)
MAX_EVIDENCE_CHARS = 48000
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
PENDING_STATUSES = ("queued", "running")
SCHEMA_UPGRADE_MESSAGE = "测评数据库结构尚未更新，请联系管理员完成数据库迁移后重试。"


@inject
@dataclass
class AgentEvaluationService(BaseService):
    """测评可见范围限定为应用所有者自己的调试与任务记录。"""

    db: SQLAlchemy
    language_model_service: LanguageModelService

    def list_sources(
        self, app_id: UUID, account: Account, current_page: int, page_size: int
    ) -> dict:
        """列表与详情使用相同权限条件，避免泄露其他会话的摘要。"""
        self._owned_app(app_id, account.id)
        query = self._source_query(app_id, account)
        total = query.count()
        rows = (
            query.order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .offset((current_page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {
            "list": [self._source(run, message, preview=True) for run, message in rows],
            "total": total,
            "current_page": current_page,
            "page_size": page_size,
        }

    def get_source(self, app_id: UUID, run_id: UUID, account: Account) -> dict:
        """只读取服务端保存的已结束运行，不接受浏览器提供的答案。"""
        self._owned_app(app_id, account.id)
        row = (
            self._source_query(app_id, account)
            .filter(AgentRun.id == run_id)
            .one_or_none()
        )
        if row is None:
            raise NotFoundException("任务不存在、尚未结束或无权测评")
        return self._source(*row)

    def submit(
        self, app_id: UUID, account: Account, req: AgentEvaluationReq
    ) -> AgentEvaluation:
        """幂等键按账号和应用隔离；提交冻结材料后交给队列，不占用 HTTP 等待评分。"""
        self._owned_app(app_id, account.id)
        evaluation_id = (
            uuid5(NAMESPACE_URL, f"evaluation:{account.id}:{app_id}:{req.request_id}")
            if req.request_id
            else uuid4()
        )
        existing = self.db.session.get(AgentEvaluation, evaluation_id)
        if existing:
            return self._reuse_submission(existing, req)
        source = self.get_source(app_id, req.run_id, account)
        if len(source["query"]) + len(source["answer"]) > MAX_EVIDENCE_CHARS:
            raise ValidateErrorException(
                "本轮文本超过 48000 字，暂不支持测评；不会截断后评分"
            )
        config = {**req.judge.model_dump(mode="json"), "parameters": {}}
        self.language_model_service.load_language_model(config, account.id)
        if req.mode == "execute":
            source["execution"] = self._execution_snapshot(app_id, account.id, req)
        try:
            evaluation = self.create(
                AgentEvaluation,
                id=evaluation_id,
                account_id=account.id,
                app_id=app_id,
                run_id=req.run_id,
                source_snapshot=source,
                criteria=list(req.criteria),
                judge_config=config,
                evaluator_version=(
                    EXECUTION_VERSION if req.mode == "execute" else EVALUATOR_VERSION
                ),
                status="queued",
                result=None,
                error_message="",
                created_at=self._now(),
                finished_at=None,
            )
        except IntegrityError as exc:
            self.db.session.rollback()
            self._check_schema_error(exc)
            if getattr(exc.orig, "pgcode", None) != "23505":
                raise
            existing = self.db.session.get(AgentEvaluation, evaluation_id)
            if existing is None:
                raise
            return self._reuse_submission(existing, req)
        self._dispatch(evaluation_id, execute=req.mode == "execute")
        self.db.session.refresh(evaluation)
        return evaluation

    def process(self, evaluation_id: UUID) -> None:
        """数据库条件更新保证重复投递最多执行一次；只使用提交时冻结的材料。"""
        # 1.先收敛过期任务并争取执行权，重复投递不再调用模型。
        evaluation = self._claim(evaluation_id)
        if evaluation is None:
            return

        # 2.执行与保存分开：数据库异常交回基础设施，模型异常写为本次测评失败。
        try:
            result = self._evaluate_snapshot(evaluation)
        except SQLAlchemyError:
            # 数据库故障交回基础设施处理，不能误写成模型评分失败。
            self.db.session.rollback()
            raise
        except Exception as exc:
            self.db.session.rollback()
            logger.warning(
                "Evaluation judge failed for %s (%s)", evaluation_id, type(exc).__name__
            )
            self._finish(
                evaluation_id,
                ("running",),
                error="评分模型调用失败或返回无效评分，请检查模型后新建测评。",
            )
            return
        # 3.模型返回后再次检查截止时间，迟到结果不能覆盖已经超时的记录。
        self._expire_pending(
            self.db.session.query(AgentEvaluation).filter(
                AgentEvaluation.id == evaluation_id
            )
        )
        self._finish(evaluation_id, ("running",), result=result)

    def list_evaluations(
        self, app_id: UUID, account: Account, current_page: int, page_size: int
    ) -> dict:
        """每次评分独立追加，修改标准再评不会覆盖旧记录。"""
        self._owned_app(app_id, account.id)
        self._expire_pending(self._evaluation_query(app_id, account.id))
        query = self._evaluation_query(app_id, account.id)
        total = query.count()
        rows = (
            query.order_by(AgentEvaluation.created_at.desc(), AgentEvaluation.id.desc())
            .offset((current_page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {
            "list": rows,
            "total": total,
            "current_page": current_page,
            "page_size": page_size,
        }

    def get_evaluation(
        self, app_id: UUID, evaluation_id: UUID, account: Account
    ) -> AgentEvaluation:
        """历史评分归属当前账号，即使源会话已删除仍保留当时的材料。"""
        self._owned_app(app_id, account.id)
        query = self._evaluation_query(app_id, account.id).filter(
            AgentEvaluation.id == evaluation_id
        )
        self._expire_pending(query)
        row = query.one_or_none()
        if row is None:
            raise NotFoundException("测评记录不存在")
        return row

    def _dispatch(self, evaluation_id: UUID, *, execute: bool) -> None:
        """只投递已提交的记录 ID；投递失败收敛排队状态，保留记录供页面说明原因。"""
        try:
            run_agent_evaluation.apply_async(
                args=[str(evaluation_id)],
                retry=False,
                expires=EVALUATION_DEADLINE_SECONDS,
                soft_time_limit=720 if execute else 240,
                time_limit=750 if execute else 270,
            )
        except Exception as exc:
            logger.warning(
                "Evaluation dispatch failed for %s (%s)",
                evaluation_id,
                type(exc).__name__,
            )
            self._finish(
                evaluation_id, ("queued",), error="测评队列暂不可用，请稍后新建测评。"
            )

    def _claim(self, evaluation_id: UUID) -> AgentEvaluation | None:
        """先检查提交截止时间，再原子认领排队记录；未取得执行权时不加载冻结材料。"""
        self._expire_pending(
            self.db.session.query(AgentEvaluation).filter(AgentEvaluation.id == evaluation_id)
        )
        with self.db.auto_commit():
            claimed = (
                self.db.session.query(AgentEvaluation)
                .filter(
                    AgentEvaluation.id == evaluation_id,
                    AgentEvaluation.status == "queued",
                )
                .update({"status": "running"}, synchronize_session=False)
            )
        if not claimed:
            return None
        evaluation = self.db.session.get(AgentEvaluation, evaluation_id)
        self.db.session.refresh(evaluation)
        return evaluation

    def _evaluate_snapshot(self, evaluation: AgentEvaluation) -> dict:
        """从冻结材料加载模型；提交读取事务后不再访问 ORM 对象，避免模型调用占用连接。"""
        evaluation_id = evaluation.id
        config, source, criteria = (
            evaluation.judge_config, evaluation.source_snapshot, evaluation.criteria,
        )
        account_id, app_id = evaluation.account_id, evaluation.app_id
        self._owned_app(app_id, account_id)
        judge_model = self.language_model_service.load_language_model(config, account_id)
        execution = source.get("execution")
        working_model = None
        if execution:
            working_model = self.language_model_service.load_language_model(
                execution["definition"]["model_config"], account_id,
            )
        self.db.session.commit()
        if not execution:
            return evaluate_answer(judge_model, source, criteria)
        runtime = execute_with_rubric(
            working_model=working_model,
            judge_model=judge_model,
            query=source["query"],
            criteria=criteria,
            execution=execution,
            evaluation_id=str(evaluation_id),
            on_progress=partial(self._save_runtime_progress, evaluation_id),
        )
        return {"runtime": runtime}

    @staticmethod
    def _check_schema_error(error: IntegrityError) -> None:
        """仅识别已知旧库约束；不解析或记录可能包含任务材料的 SQL 错误正文。"""
        diagnostic = getattr(error.orig, "diag", None)
        if (
            getattr(error.orig, "pgcode", None) == "23502"
            and getattr(diagnostic, "table_name", None) == "agent_evaluation"
            and getattr(diagnostic, "column_name", None) == "finished_at"
        ):
            logger.warning("Agent evaluation schema requires nullable finished_at")
            raise FailException(SCHEMA_UPGRADE_MESSAGE) from None

    def _execution_snapshot(
        self, app_id: UUID, account_id: UUID, req: AgentEvaluationReq
    ) -> dict:
        """只读取现有草稿并冻结模型/提示词，不触发应用属性的隐式创建。"""
        draft = (
            self.db.session.query(AppConfigVersion)
            .filter(
                AppConfigVersion.app_id == app_id,
                AppConfigVersion.config_type == AppConfigType.DRAFT,
            )
            .one_or_none()
        )
        if draft is None:
            raise ValidateErrorException("当前 Agent 没有草稿配置，请先保存 Agent 配置")
        model_config = AppModelConfigReq.model_validate(draft.model_config).model_dump(
            mode="json", by_alias=True
        )
        self.language_model_service.load_language_model(model_config, account_id)
        return {
            "mode": "execute",
            "max_iterations": req.max_iterations,
            "evidence_rules": [rule.model_dump() for rule in req.evidence_rules],
            "scope": EXECUTION_SCOPE,
            "definition": {
                "config_id": str(draft.id),
                "model_config": model_config,
                "preset_prompt": draft.preset_prompt or "",
                "review_config": draft.review_config or {},
            },
        }

    def _save_runtime_progress(self, evaluation_id: UUID, progress: dict) -> None:
        """回调之外持久化每轮证据；保存失败会终止运行，不能继续自动修订。"""
        with self.db.auto_commit():
            changed = (
                self.db.session.query(AgentEvaluation)
                .filter(
                    AgentEvaluation.id == evaluation_id,
                    AgentEvaluation.status == "running",
                )
                .update({"result": {"runtime": progress}}, synchronize_session=False)
            )
        if not changed:
            raise RuntimeError("测评记录已结束，停止后续评分")

    def _reuse_submission(
        self, evaluation: AgentEvaluation, req: AgentEvaluationReq
    ) -> AgentEvaluation:
        """同一幂等键不能悄悄绑定到另一份标准或模型。"""
        config = {**req.judge.model_dump(mode="json"), "parameters": {}}
        execution = evaluation.source_snapshot.get("execution")
        same_mode = bool(execution) == (req.mode == "execute")
        same_options = not execution or (
            execution["max_iterations"] == req.max_iterations
            and execution["evidence_rules"]
            == [rule.model_dump() for rule in req.evidence_rules]
        )
        if (
            not same_mode
            or not same_options
            or evaluation.run_id != req.run_id
            or evaluation.criteria != req.criteria
            or evaluation.judge_config != config
        ):
            raise ValidateErrorException("提交标识已用于不同的测评，请重新提交")
        self._expire_pending(
            self.db.session.query(AgentEvaluation).filter(
                AgentEvaluation.id == evaluation.id
            )
        )
        self.db.session.refresh(evaluation)
        return evaluation

    def _finish(
        self,
        evaluation_id: UUID,
        statuses: tuple[str, ...],
        *,
        result: dict | None = None,
        error: str = "",
    ) -> None:
        """迟到的模型返回不能覆盖已超时的记录。"""
        values = {
            "status": "completed" if result is not None else "error",
            "error_message": error,
            "finished_at": self._now(),
        }
        # 已完成轮次是独立证据；后续模型异常时仍可查看，不能抹掉进度。
        if result is not None:
            values["result"] = result
        with self.db.auto_commit():
            self.db.session.query(AgentEvaluation).filter(
                AgentEvaluation.id == evaluation_id,
                AgentEvaluation.status.in_(statuses),
            ).update(
                values,
                synchronize_session=False,
            )

    def _expire_pending(self, query: Query) -> None:
        """只收敛调用者指定范围内的过期记录，不在 Worker 或详情轮询中扫描全库。"""
        now = self._now()
        with self.db.auto_commit():
            query.filter(
                AgentEvaluation.status.in_(PENDING_STATUSES),
                AgentEvaluation.created_at
                < now - timedelta(seconds=EVALUATION_DEADLINE_SECONDS),
            ).update(
                {
                    "status": "error",
                    "error_message": "测评等待或执行超过 15 分钟，请检查后台队列后新建测评。",
                    "finished_at": now,
                },
                synchronize_session="fetch",
            )

    def _evaluation_query(self, app_id: UUID, account_id: UUID) -> Query:
        """历史列表和详情共用账号隔离条件。"""
        return self.db.session.query(AgentEvaluation).filter(
            AgentEvaluation.app_id == app_id,
            AgentEvaluation.account_id == account_id,
        )

    @staticmethod
    def _now() -> datetime:
        """数据库沿用无时区 UTC，接口统一按 UTC 展示。"""
        return datetime.now(timezone.utc).replace(tzinfo=None)

    def _owned_app(self, app_id: UUID, account_id: UUID) -> None:
        """应用入口不能用于跨账号查询测评。"""
        if (
            self.db.session.query(App.id)
            .filter(App.id == app_id, App.account_id == account_id)
            .one_or_none()
            is None
        ):
            raise NotFoundException("应用不存在或无权访问")

    def _source_query(self, app_id: UUID, account: Account):
        """仅纳入本人任务/调试会话，兼容尚未写 owner_account_id 的旧记录。"""
        return (
            self.db.session.query(AgentRun, Message)
            .join(
                Message,
                and_(
                    Message.id == AgentRun.message_id,
                    Message.app_id == AgentRun.app_id,
                    Message.conversation_id == AgentRun.conversation_id,
                ),
            )
            .join(Conversation, Conversation.id == AgentRun.conversation_id)
            .filter(
                AgentRun.app_id == app_id,
                AgentRun.status.in_(TERMINAL_STATUSES),
                AgentRun.invoke_from.in_(("debugger", "creator_task")),
                Conversation.app_id == app_id,
                Conversation.is_deleted.is_(False),
                Message.is_deleted.is_(False),
                or_(
                    Conversation.owner_account_id == account.id,
                    and_(
                        Conversation.owner_account_id.is_(None),
                        Conversation.created_by == account.id,
                    ),
                ),
            )
        )

    @staticmethod
    def _source(run: AgentRun, message: Message, preview: bool = False) -> dict:
        """明确评分材料范围，不把 Agent 自述当作外部执行证据。"""
        source = {
            "run_id": str(run.id),
            "conversation_id": str(run.conversation_id),
            "query": (message.query or "")[:160] if preview else message.query or "",
            "status": run.status,
            "invoke_from": run.invoke_from,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "scope": EVIDENCE_SCOPE,
        }
        if not preview:
            source["answer"] = message.answer or ""
        return source
