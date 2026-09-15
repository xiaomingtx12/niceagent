"""为原生异步任务补充面向用户的名称和说明，调度仍由 DeepAgents 负责。"""

import re
from collections.abc import Callable
from dataclasses import replace
from typing import Annotated, Any, NotRequired, cast

from deepagents.middleware.async_subagents import (
    AsyncSubAgent,
    AsyncSubAgentMiddleware,
    AsyncTask,
    StartAsyncTaskSchema,
)
from langchain.agents.middleware.types import AgentState
from langchain.tools import ToolRuntime
from langchain_core.tools import StructuredTool
from langgraph.types import Command
from pydantic import Field, StringConstraints


class NamedAsyncTask(AsyncTask):
    """执行者沿用 agent_name；title 和 description 描述本次派发的具体工作。"""

    title: str
    description: str


def _merge_tasks(
    existing: dict[str, NamedAsyncTask] | None,
    updates: dict[str, NamedAsyncTask],
) -> dict[str, NamedAsyncTask]:
    """原生查询、取消和追加指令会重建任务记录，仅保留平台增加的描述字段。"""
    merged = dict(existing or {})
    for task_id, task in updates.items():
        previous = merged.get(task_id, {})
        details = {key: previous[key] for key in ("title", "description") if key in previous}
        merged[task_id] = cast(NamedAsyncTask, {**details, **task})
    return merged


class NamedAsyncTaskState(AgentState):
    """在现有 async_tasks 通道合并描述信息，同时保留并行任务各自的更新。"""

    async_tasks: Annotated[NotRequired[dict[str, NamedAsyncTask]], _merge_tasks]


class StartNamedAsyncTaskReq(StartAsyncTaskSchema):
    """名称是展示信息，允许省略；完整任务要求仍由派发方提供。"""

    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] = Field(
        description="交给子 Agent 的完整任务要求，包含目标、必要上下文和预期产出，不能仅填写任务名称。",
    )
    title: Annotated[
        str,
        StringConstraints(strip_whitespace=True, max_length=80, pattern=r"^[^\r\n]*$"),
    ] | None = Field(
        default=None,
        description="建议提供本次任务的简短名称，8–20 字，概括目标和对象，例如：调研 Celery 部署方案。省略时从 description 提取标题。",
    )


class NamedAsyncSubAgentMiddleware(AsyncSubAgentMiddleware):
    """只扩展启动参数和任务描述的保存，复用原生传输、工具与系统提示词。"""

    state_schema = NamedAsyncTaskState

    def __init__(self, *, async_subagents: list[AsyncSubAgent]) -> None:
        """每个图持有自己的工具副本，避免修改依赖包或共享工具对象。"""
        super().__init__(async_subagents=async_subagents)
        self.tools = [
            _named_start_tool(tool) if tool.name == "start_async_task" else tool
            for tool in self.tools
        ]


def _named_start_tool(tool: StructuredTool) -> StructuredTool:
    """同步和异步入口均调用原生启动函数，成功后在同一 Command 中保存描述。"""
    start = cast(Callable, tool.func)
    astart = cast(Callable, tool.coroutine)

    def start_named_task(
        description: str, subagent_type: str, runtime: ToolRuntime, title: str | None = None,
    ) -> str | Command:
        """名称校验由工具 Schema 在远程任务创建前完成。"""
        result = start(description=description, subagent_type=subagent_type, runtime=runtime)
        return _with_task_details(result, title=title, description=description)

    async def astart_named_task(
        description: str, subagent_type: str, runtime: ToolRuntime, title: str | None = None,
    ) -> str | Command:
        """异步宿主使用原生异步传输，不转入同步客户端。"""
        result = await astart(description=description, subagent_type=subagent_type, runtime=runtime)
        return _with_task_details(result, title=title, description=description)

    return StructuredTool.from_function(
        name=tool.name,
        description=tool.description + "\n用 description 提供完整任务要求，建议同时提供独立的简短 title；省略 title 不影响任务执行。",
        func=start_named_task,
        coroutine=astart_named_task,
        args_schema=StartNamedAsyncTaskReq,
        infer_schema=False,
    )


def _with_task_details(result: str | Command, *, title: str | None, description: str) -> str | Command:
    """失败回执不产生任务；成功回执保留原有消息、调度信息和任务标识。"""
    if not isinstance(result, Command):
        return result
    display_title = title or _title_from_description(description)
    update = cast(dict[str, Any], result.update)
    tasks = {
        task_id: {**task, "title": display_title, "description": description}
        for task_id, task in update["async_tasks"].items()
    }
    return replace(result, update={**update, "async_tasks": tasks})


def _title_from_description(description: str) -> str:
    """仅提取原说明的首句作为展示标题，不额外请求模型或改写执行要求。"""
    text = description.removeprefix("任务：").removeprefix("任务:").strip()
    first_sentence = re.split(r"[\r\n。！？!?]", text, maxsplit=1)[0].strip()
    return " ".join((first_sentence or description).split())[:80]
