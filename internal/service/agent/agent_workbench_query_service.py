"""工作台只读视图与事件订阅，不创建运行或提交业务状态。"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
import time
from typing import Any
from uuid import UUID

from injector import inject
from internal.entity import WORKBENCH_ACTIVE_STATUSES, WORKBENCH_TERMINAL_STATUSES
from internal.exception import NotFoundException
from internal.model import Account, AgentRun
from internal.entity.agent_invocation import (
    AgentInvocationContext,
)
from internal.schema.agent.agent_model_schema import (
    WorkbenchModelSettingsResp,
)
from internal.schema.agent.agent_workbench_read_schema import WorkbenchRunEventsResp, WorkbenchStateView
from internal.schema.agent.agent_workbench_schema import (
    WorkbenchEventStreamReq, WorkbenchStateResp, WorkbenchRunObservabilityResp,
    WorkbenchMessagePageInfoResp, WorkbenchRunMetadataResp,
)
from internal.service.agent_protocol.message_projector import project_conversation_messages, merge_running_messages
from internal.service.agent_protocol.state_projector import project_run_history, project_snapshot_values, project_pending_tasks
from internal.service.conversation.conversation_service import ConversationService
from pkg.sqlalchemy import SQLAlchemy
from .agent_model_service import AgentModelService
from .agent_workbench_event_queue import AgentWorkbenchEventQueueManager
from .agent_workbench_run_service import AgentWorkbenchRunService


@inject
@dataclass
class AgentWorkbenchQueryService:
    """查询权限沿用可信上下文；快照和 SSE 都只读取已提交数据。"""

    db: SQLAlchemy
    run_service: AgentWorkbenchRunService
    queue_manager: AgentWorkbenchEventQueueManager
    model_service: AgentModelService
    conversation_service: ConversationService

    _SSE_HEARTBEAT_SECONDS = 15

    def subscribe(
        self,
        *,
        context: AgentInvocationContext,
        stream_request: WorkbenchEventStreamReq,
    ) -> Iterator[dict[str, Any] | None]:
        """创建事件流；数据库 Session 在流开始前释放，避免占用请求连接。"""
        # 1.权限校验已在 Handler 调用上下文时完成；这里释放请求 Session，避免流连接长期占用。
        run_id = stream_request.run_id
        self.run_service.get_context_run(context, run_id)
        self.db.session.close()
        # 2.返回延迟执行的生成器，首批事件会在 SSE 响应开始迭代后读取。
        return self._stream_events(
            context=context, stream_request=stream_request, run_id=run_id
        )

    def get_state(
        self,
        *,
        context: AgentInvocationContext,
        view: WorkbenchStateView = "full",
    ) -> WorkbenchStateResp:
        """使用入口已校验的上下文读取工作台状态。"""
        if view == "status":
            metadata = self.run_service.get_run_metadata(
                conversation_id=context.conversation_id, invoke_from=context.invoke_from,
            )
            return WorkbenchStateResp(
                metadata=metadata,
                next=["agent"] if metadata.status in WORKBENCH_ACTIVE_STATUSES else [],
            )
        # 1.由会话服务读取业务消息和稳定游标，再关联对应的 Agent Run。
        rows, has_more, next_before = self.conversation_service.get_message_page(
            conversation_id=context.conversation_id,
            page_size=20,
        )
        message_page = WorkbenchMessagePageInfoResp(
            has_more=has_more,
            next_before=next_before,
        )
        message_ids = [message.id for message in rows]
        persisted_runs = self.run_service.list_state_runs(
            conversation_id=context.conversation_id,
            invoke_from=context.invoke_from,
            message_ids=message_ids,
        )
        messages = project_conversation_messages(
            rows, runs_by_message={run.message_id: run for run in persisted_runs}
        )
        latest_run = persisted_runs[-1] if persisted_runs else None

        # 2.只读取已提交的快照，状态查询与跨进程 SSE 使用同一数据源。
        source_snapshot: Mapping[str, Any] = {}
        if latest_run is not None and isinstance(latest_run.state_snapshot, Mapping):
            source_snapshot = latest_run.state_snapshot
        values = project_snapshot_values(source_snapshot)
        values["messages"] = [
            message.model_dump(mode="json") for message in messages
        ]
        values["messages"] = merge_running_messages(
            values["messages"], latest_run, source_snapshot,
        )
        values["message_page"] = message_page.model_dump(mode="json")

        # 3.恢复运行详情；合成快照仅用于水合，不参与 SSE 游标。
        values.update(self._project_state_events(persisted_runs, view=view))

        # 4.当前运行优先，找不到运行记录时返回空闲状态。
        if latest_run is None:
            run_id, status, last_event_sequence = None, "idle", 0
        else:
            run_id = latest_run.id
            status = str(latest_run.status)
            last_event_sequence = int(latest_run.last_event_sequence or 0)
        # 5.等待输入时才生成待办表单，避免普通状态响应构造无效任务。
        tasks = project_pending_tasks(status, values)
        return WorkbenchStateResp(
            values=values,
            next=["agent"] if status in WORKBENCH_ACTIVE_STATUSES else [],
            tasks=tasks,
            metadata=WorkbenchRunMetadataResp(
                run_id=run_id,
                status=status,
                last_event_sequence=last_event_sequence,
            ),
        )

    def get_run_events(
        self, *, run_id: UUID, account: Account, current_page: int, page_size: int,
    ) -> WorkbenchRunEventsResp:
        """沿用观测接口的会话所有权校验，详情查询不执行失联恢复。"""
        row = (
            self.db.session.query(AgentRun.app_id, AgentRun.conversation_id)
            .filter(AgentRun.id == run_id)
            .one_or_none()
        )
        if row is None:
            raise NotFoundException("Agent Run 不存在")
        self.conversation_service.get_owned_conversation(
            conversation_id=row.conversation_id, app_id=row.app_id, account=account,
        )
        return self.run_service.get_run_event_page(
            run_id=run_id, current_page=current_page, page_size=page_size,
        )

    def get_model_settings(self, *, context: AgentInvocationContext) -> WorkbenchModelSettingsResp:
        """独立读取当前可用模型配置，恢复最近一次覆盖时不加载消息和运行事件。"""
        saved = self.run_service.get_latest_model_overrides(
            conversation_id=context.conversation_id,
            invoke_from=context.invoke_from,
        )
        return self.model_service.restore_workbench_model_settings(context, saved)

    def get_run_observability(
        self,
        *,
        run_id: UUID,
        account: Account,
    ) -> WorkbenchRunObservabilityResp:
        """鉴权后读取平台持久化的 Agent Run 观测链路。"""
        # 1.读取并校验目标 Run 的稳定归属。
        row = self.run_service.get_run(run_id, recover=False)
        if row is None:
            raise NotFoundException("Agent Run 不存在")
        self.conversation_service.get_owned_conversation(
            conversation_id=row.conversation_id,
            account=account,
            app_id=row.app_id,
        )
        # 2.投影平台保存的观测快照，保持查询接口只读。
        return WorkbenchRunObservabilityResp(
            run_id=row.id,
            trace_id=row.id,
            status=str(row.status),
            started_at=row.started_at,
            finished_at=row.finished_at,
            observability=dict(row.observability or {}),
        )

    def _stream_events(
        self,
        *,
        context: AgentInvocationContext,
        stream_request: WorkbenchEventStreamReq,
        run_id: UUID,
    ) -> Iterator[dict[str, Any] | None]:
        """读取已提交事件并产生 SSE 心跳哨兵，队列管理器只负责事件通知。"""
        # 1.从客户端游标开始续传；游标只根据已扫描的事件批次推进。
        cursor = stream_request.since
        heartbeat_at = time.monotonic() + self._SSE_HEARTBEAT_SECONDS
        with self.queue_manager.watch_events(
            context.conversation_id, context.invoke_from
        ) as signal:
            while True:
                # 2.先记录通知版本，再查询数据库，避免查询期间的提交被漏掉。
                version = signal.snapshot()
                try:
                    # 3.共享已提交事件批次；失联 Run 由后台 sweeper 收敛。
                    batch = signal.read_events(
                        cursor,
                        load=lambda: self.run_service.get_reconnect_events(
                            conversation_id=context.conversation_id,
                            invoke_from=context.invoke_from,
                            run_id=run_id,
                            since=cursor,
                        ),
                    )
                finally:
                    # 4.每批查询结束立即释放线程 Session，下一批读取时重新获取连接。
                    self.db.session.close()

                # 5.过滤只影响发送内容，不影响内部游标，避免被过滤事件反复扫描。
                for event in batch.events:
                    if stream_request.matches(event):
                        yield event.model_dump(mode="json")
                terminal = any(
                    event.method == "lifecycle"
                    and isinstance(event.params.data, Mapping)
                    and event.params.data.get("status") in WORKBENCH_TERMINAL_STATUSES
                    for event in batch.events
                )
                cursor = batch.next_sequence

                if time.monotonic() >= heartbeat_at:
                    # 6.心跳仅维持 SSE 连接；None 由响应适配器编码为注释行，不进入业务事件流。
                    yield None
                    heartbeat_at = time.monotonic() + self._SSE_HEARTBEAT_SECONDS
                if not batch.has_more:
                    if terminal:
                        return
                    if not batch.events:
                        status = self.run_service.get_run_status(run_id)
                        if status in WORKBENCH_TERMINAL_STATUSES:
                            return
                    # 7.当前批次已读完才等待通知；仍有后续批次时立即继续读取。
                    signal.wait(version, timeout=0.5)

    def _project_state_events(
        self,
        persisted_runs: list[AgentRun],
        *,
        view: WorkbenchStateView,
    ) -> dict[str, Any]:
        """按视图读取运行事件；compact 终态只读取上下文事件摘要。"""
        deferred_ids: set[UUID] = set()
        if view == "compact":
            deferred_ids = {
                run.id
                for run in persisted_runs
                if run.status in WORKBENCH_TERMINAL_STATUSES
            }

        event_run_ids = [run.id for run in persisted_runs if run.id not in deferred_ids]
        events_by_run = self.run_service.list_events_by_run(event_run_ids)
        if deferred_ids:
            events_by_run.update(
                self.run_service.list_context_events_by_run(list(deferred_ids))
            )

        result = {
            "workbench_events": [
                event.model_dump(mode="json")
                for event in project_run_history(persisted_runs, events_by_run)
            ]
        }
        if view == "compact":
            result["deferred_run_ids"] = [
                str(run.id) for run in persisted_runs if run.id in deferred_ids
            ]
        return result
