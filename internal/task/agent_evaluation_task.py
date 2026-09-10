"""后台评分仅接收记录标识，不通过消息队列传递任务文本或凭证。"""

from uuid import UUID

from celery import shared_task


@shared_task(soft_time_limit=240, time_limit=270, ignore_result=True)
def run_agent_evaluation(evaluation_id: str) -> None:
    """沿用应用注入器，在 Celery 的 Flask 上下文内执行。"""
    from app.http.module import injector
    from internal.service.agent_evaluation_service import AgentEvaluationService

    injector.get(AgentEvaluationService).process(UUID(evaluation_id))
