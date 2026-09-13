"""工作台分页执行详情 DTO。"""

from uuid import UUID
from pydantic import BaseModel
from internal.schema.agent.agent_workbench_schema import WorkbenchEvent


class WorkbenchRunEventsResp(BaseModel):
    """单个 Run 的执行详情按稳定序号升序分页。"""

    run_id: UUID
    events: list[WorkbenchEvent]
    next_page: int | None = None
