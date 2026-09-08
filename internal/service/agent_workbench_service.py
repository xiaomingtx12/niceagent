"""工作台运行启动、状态查询、取消和事件订阅。"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from flask import Flask, current_app
from injector import inject

from internal.service.agent_protocol.message_projector import (
    project_conversation_messages,
)
from internal.entity import (
    WORKBENCH_ACTIVE_STATUSES,
    WORKBENCH_TERMINAL_STATUSES,
    WorkbenchRun,
    WorkbenchRunInput,
    WorkbenchRunStatus,
)
from internal.exception import NotFoundException
from internal.extension.logging_extension import logger
from internal.model import Account, AgentRun, Conversation
from internal.schema.agent_attachment_schema import dump_agent_attachments
from internal.schema.agent_runtime_schema import AgentInvocation, AgentInvocationContext
from internal.schema.agent_workbench_schema import (
    WorkbenchCancelResp,
    WorkbenchCommandErrorResp,
    WorkbenchCommandSuccessResp,
    WorkbenchEvent,
    WorkbenchRunObservabilityResp,
    WorkbenchMessagePageInfoResp,
    WorkbenchRunMetadataResp,
    WorkbenchStateResp,
    WorkbenchEventStreamReq,
)
from pkg.sqlalchemy import SQLAlchemy

from .agent_attachment_service import AgentAttachmentService
from .agent_workbench_event_queue import AgentWorkbenchEventQueueManager
from .agent_workbench_execution_service import AgentWorkbenchRunExecutionService
from .agent_workbench_run_service import (
    AgentWorkbenchRunService,
    build_state_snapshot_event,
)
from .conversation_service import ConversationService

_STATE_SNAPSHOT_KEYS = ("todos", "files", "async_tasks")


@inject
@dataclass
class AgentWorkbenchService:
    """消费可信调用上下文，编排 Run、状态、取消和事件订阅。"""

    db: SQLAlchemy
    run_service: AgentWorkbenchRunService
    queue_manager: AgentWorkbenchEventQueueManager
    execution_service: AgentWorkbenchRunExecutionService
    attachment_service: AgentAttachmentService
    conversation_service: ConversationService

    def subscribe(
        self,
        *,
        context: AgentInvocationContext,
        stream_request: WorkbenchEventStreamReq,
    ) -> Iterator[dict[str, Any] | None]:
        """使用已提交事件续传，跨进程订阅和快速结束的 Run 走同一条路径。"""
        try:
            replay_events = self.run_service.get_reconnect_events(
                conversation_id=context.conversation_id,
                invoke_from=context.invoke_from,
                since=stream_request.since,
            )
        finally:
            self.db.session.close()

        def stream_events() -> Iterator[dict[str, Any] | None]:
            """每批读取后释放 Session；过滤事件同样推进内部扫描游标。"""
            cursor = stream_request.since
            events = replay_events
            heartbeat_at = time.monotonic() + 15
            while True:
                for event in events:
                    cursor = event.seq
                    if stream_request.matches(event):
                        yield event.model_dump(mode="json")
                if len(events) < 256:
                    self.queue_manager.wait_for_events(timeout=0.5)
                if time.monotonic() >= heartbeat_at:
                    yield None
                    heartbeat_at = time.monotonic() + 15
                try:
                    events = self.run_service.get_reconnect_events(
                        conversation_id=context.conversation_id,
                        invoke_from=context.invoke_from,
                        since=cursor,
                    )
                finally:
                    self.db.session.close()

        return stream_events()

    def get_state(
        self,
        *,
        context: AgentInvocationContext,
    ) -> WorkbenchStateResp:
        """使用入口已校验的上下文读取工作台状态。"""
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
        persisted_runs = self.run_service.list_runs_for_messages(
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
        values = {
            key: source_snapshot[key]
            for key in _STATE_SNAPSHOT_KEYS
            if key in source_snapshot
        }
        values["messages"] = [message.model_dump(mode="json") for message in messages]
        if latest_run is not None and latest_run.status in WORKBENCH_ACTIVE_STATUSES:
            current_ids = {
                str(latest_run.input_message_id),
                f"{latest_run.message_id}:human",
                f"{latest_run.message_id}:assistant",
            }
            pending_messages = [
                message
                for message in source_snapshot.get("messages", [])
                if isinstance(message, dict) and message.get("id") in current_ids
            ]
            by_id = {message["id"]: message for message in values["messages"]}
            by_id.update({message["id"]: message for message in pending_messages})
            values["messages"] = list(by_id.values())
        values["message_page"] = message_page.model_dump(mode="json")

        # 3.恢复运行详情；合成快照仅用于水合，不参与 SSE 游标。
        events_by_run = self.run_service.list_events_by_run(
            [run.id for run in persisted_runs]
        )
        historical_events: list[WorkbenchEvent] = []
        for run in persisted_runs:
            historical_events.extend(events_by_run.get(run.id, []))
            snapshot_event = build_state_snapshot_event(
                run.id,
                int(run.last_event_sequence or 0),
                run.state_snapshot,
            )
            if snapshot_event is not None:
                historical_events.append(snapshot_event)
            if run.status in WORKBENCH_TERMINAL_STATUSES:
                historical_events.append(
                    WorkbenchEvent(
                        type="event",
                        event_id=f"{run.id}:terminal-hydration",
                        seq=int(run.last_event_sequence or 0),
                        method="lifecycle",
                        params={
                            "data": {
                                "event": (
                                    "interrupted"
                                    if run.status == "cancelled"
                                    else run.status
                                ),
                                "status": run.status,
                                "message_id": str(run.message_id),
                                "input_message_id": run.input_message_id,
                                **(
                                    {"error": "Agent 运行失败"}
                                    if run.status == "failed"
                                    else {}
                                ),
                            }
                        },
                    )
                )

        values["workbench_events"] = [
            event.model_dump(mode="json")
            for event in sorted(historical_events, key=lambda item: item.seq)
        ]

        # 4.当前运行优先，找不到运行记录时返回空闲状态。
        if latest_run is None:
            run_id, status, last_event_sequence = None, "idle", 0
        else:
            run_id = latest_run.id
            status = str(latest_run.status)
            last_event_sequence = int(latest_run.last_event_sequence or 0)
        return WorkbenchStateResp(
            values=values,
            next=["agent"] if status in WORKBENCH_ACTIVE_STATUSES else [],
            tasks=[],
            metadata=WorkbenchRunMetadataResp(
                run_id=run_id,
                status=status,
                last_event_sequence=last_event_sequence,
            ),
        )

    def cancel_run(
        self,
        *,
        context: AgentInvocationContext,
        run_id: UUID,
    ) -> WorkbenchCancelResp:
        """使用入口已校验的上下文停止指定 Agent Run。"""
        # 1.校验目标 Run 是否属于本次请求的会话和入口。
        row = self.run_service.get_run(run_id)
        if (
            row is None
            or row.app_id != context.app_id
            or row.conversation_id != context.conversation_id
            or row.invoke_from != context.invoke_from.value
        ):
            raise NotFoundException("Agent Run 不存在或不属于当前会话")
        # 2.先提交数据库取消状态，再通知本地执行线程。
        cancel_requested = False
        if (
            row.status not in WORKBENCH_TERMINAL_STATUSES
            and self.run_service.request_cancel(row)
        ):
            cancel_requested = True
            self.queue_manager.request_cancel(
                context.conversation_id,
                row.id,
                app_id=context.app_id,
                invoke_from=context.invoke_from,
            )
        return WorkbenchCancelResp(
            cancel_requested=cancel_requested,
            run_id=row.id,
            status=(
                WorkbenchRunStatus.CANCELLING.value
                if cancel_requested
                else str(row.status)
            ),
        )

    def get_run_observability(
        self,
        *,
        run_id: UUID,
        account: Account,
    ) -> WorkbenchRunObservabilityResp:
        """鉴权后读取平台持久化的 Agent Run 观测链路。"""
        # 1.读取并校验目标 Run 的稳定归属。
        row = self.run_service.get_run(run_id)
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

    def start_run(
        self,
        *,
        context: AgentInvocationContext,
        run_input: WorkbenchRunInput,
        command_id: int | None = None,
    ) -> WorkbenchCommandSuccessResp | WorkbenchCommandErrorResp:
        """锁定会话后完成去重和创建，提交成功才注册并启动后台执行。"""
        conversation_id = context.conversation_id

        with self.db.auto_commit():
            # 1.锁定会话行，使同一会话的启动请求按顺序检查和创建。
            conversation = (
                self.db.session.query(Conversation.id)
                .filter(
                    Conversation.id == conversation_id,
                    Conversation.app_id == context.app_id,
                    Conversation.is_deleted.is_(False),
                )
                .with_for_update()
                .one_or_none()
            )
            if conversation is None:
                raise NotFoundException("会话不存在或已删除")

            # 2.重复提交同一输入时直接返回已有 Run。
            duplicate_run = self.run_service.get_run_by_input_message(
                conversation_id=conversation_id,
                input_message_id=run_input.input_message_id,
            )
            if duplicate_run is not None:
                if duplicate_run.invoke_from != context.invoke_from.value:
                    return WorkbenchCommandErrorResp(
                        id=command_id, message="输入消息已用于其他调用入口"
                    )
                local_run = self.queue_manager.get(conversation_id)
                applied_sequence = self.run_service.get_initial_event_sequence(
                    duplicate_run
                )
                if local_run is not None and local_run.run_id == duplicate_run.id:
                    applied_sequence = local_run.initial_event_sequence
                return WorkbenchCommandSuccessResp.create(
                    command_id=command_id,
                    run_id=duplicate_run.id,
                    applied_through_sequence=applied_sequence,
                )

            # 3.取消中的任务仍占用会话；本地资源尚未关闭时也不启动下一轮。
            active_run = (
                self.db.session.query(AgentRun)
                .filter(
                    AgentRun.conversation_id == conversation_id,
                    AgentRun.status.in_(WORKBENCH_ACTIVE_STATUSES),
                )
                .first()
            )
            local_run = self.queue_manager.get(conversation_id)
            if active_run is not None or (
                local_run is not None and not local_run.done.is_set()
            ):
                cancelling = (
                    active_run is not None
                    and active_run.status == WorkbenchRunStatus.CANCELLING.value
                ) or (local_run is not None and local_run.cancel_requested.is_set())
                return WorkbenchCommandErrorResp(
                    id=command_id,
                    message=(
                        "当前任务正在停止，请稍后重试"
                        if cancelling
                        else "当前任务已有正在执行的 Agent"
                    ),
                )

            # 4.解析附件并创建业务消息，与 Run 一起提交或回滚。
            attachments = self.attachment_service.resolve(
                run_input.file_ids,
                owner_id=context.account_id,
            )
            message = self.conversation_service.create_agent_message(
                context=context,
                query=run_input.query,
                image_urls=run_input.image_urls,
                attachments=dump_agent_attachments(attachments),
            )
            invocation = AgentInvocation(
                **context.model_dump(),
                message_id=message.id,
                query=run_input.query,
                image_urls=run_input.image_urls,
                attachments=attachments,
                selected_skills=run_input.selected_skills,
                input_message_id=run_input.input_message_id,
            )
            sequence = self.run_service.get_last_event_sequence(
                conversation_id=conversation_id,
                invoke_from=context.invoke_from,
                close_session=False,
            )
            run = WorkbenchRun(
                run_id=uuid4(),
                conversation_id=conversation_id,
                spec=invocation,
                initial_event_sequence=sequence,
                last_event_sequence=sequence,
            )
            self.run_service.add_run(run)

        # 5.提交成功后登记本地 Run，并在独立 Flask 上下文中启动执行。
        try:
            self.queue_manager.register(run)
            threading.Thread(
                target=self._execute_background_run,
                args=(current_app._get_current_object(), run),
                name=f"agent-workbench-{run.run_id}",
                daemon=True,
            ).start()
        except Exception as exc:
            self._fail_background_run(run, exc)
            raise

        return WorkbenchCommandSuccessResp.create(
            command_id=command_id,
            run_id=run.run_id,
            applied_through_sequence=sequence,
        )

    def _execute_background_run(self, flask_app: Flask, run: WorkbenchRun) -> None:
        """后台线程使用独立应用上下文和 Session，退出时兜底收敛失败状态。"""
        with flask_app.app_context():
            try:
                self.execution_service.execute(run)
            except Exception as exc:
                self._fail_background_run(run, exc)
            finally:
                self.db.session.remove()

    def _fail_background_run(self, run: WorkbenchRun, exc: Exception) -> None:
        """后台执行异常时保证数据库状态和本地队列都进入终态。"""
        run.error_message = str(exc)
        event = WorkbenchEvent(
            type="event",
            event_id=f"{run.run_id}:{run.last_event_sequence + 1}",
            seq=run.last_event_sequence + 1,
            method="lifecycle",
            params={
                "data": {
                    "event": "failed",
                    "status": "failed",
                    "error": "Agent 运行失败",
                    "message_id": str(run.spec.message_id),
                    "input_message_id": run.spec.input_message_id,
                }
            },
        )
        try:
            committed = self.run_service.finalize_run(
                run,
                WorkbenchRunStatus.FAILED,
                error_message=str(exc),
                message_updates={"status": "error", "error": "Agent 运行失败"},
                events=[event],
            )
            if committed:
                run.status = WorkbenchRunStatus.FAILED
                run.last_event_sequence = event.seq
                self.queue_manager.notify_events()
        except Exception:
            logger.exception("保存 Agent Run 后台失败状态失败")
        finally:
            self.queue_manager.finish(run)
        logger.error(
            "Agent 工作台后台执行失败",
            exc_info=(type(exc), exc, exc.__traceback__),
        )


__all__ = ["AgentWorkbenchService"]
