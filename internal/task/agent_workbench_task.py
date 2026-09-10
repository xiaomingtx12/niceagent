"""Agent 工作台后台维护任务。"""

from celery import shared_task


@shared_task(ignore_result=True)
def recover_stale_agent_runs() -> None:
    """定期收敛失联 Run，避免依赖用户访问接口触发恢复。"""
    from app.http.module import injector
    from internal.service.agent.agent_workbench_run_service import AgentWorkbenchRunService

    injector.get(AgentWorkbenchRunService).recover_stale_runs()

