"""工作台运行启动、状态查询、取消和事件订阅。"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from flask import Flask, current_app
from injector import inject

from internal.service.agent_protocol.message_projector import (
    project_conversation_messages,
)
from internal.service.agent_protocol.state_projector import project_snapshot_values, project_run_history
from internal.entity import (
    WORKBENCH_ACTIVE_STATUSES,
    WORKBENCH_TERMINAL_STATUSES,
    WorkbenchRun,
    WorkbenchRunInput,
    WorkbenchRunStatus,
)
from internal.exception import NotFoundException, ValidateErrorException
from internal.core.agent_v2.entity.user_input import PendingUserInput
from internal.extension.logging_extension import logger
from internal.model import Account, AgentRun, AgentRunEvent, Conversation, Message
from internal.service.agent_protocol.event_projector import AgentProtocolEventProjector
from internal.schema.agent.agent_attachment_schema import dump_agent_attachments
from internal.schema.agent.agent_workbench_read_schema import WorkbenchRunEventsResp, WorkbenchStateView
from internal.schema.agent.agent_runtime_schema import AgentInvocation, AgentInvocationContext, WorkbenchModelSettingsResp
from internal.entity.agent_workbench_entity import WorkbenchModelSelection
from internal.schema.agent.agent_workbench_schema import (
    WorkbenchCancelResp,
    WorkbenchCommandErrorResp,
    WorkbenchCommandSuccessResp,
    WorkbenchEvent,
    WorkbenchRunObservabilityResp,
    WorkbenchMessagePageInfoResp,
    WorkbenchRunMetadataResp,
    WorkbenchStateResp,
    WorkbenchEventStreamReq,
    WorkbenchInputRespondReq,
    WorkbenchInputReopenReq,
)
from pkg.sqlalchemy import SQLAlchemy

from .agent_attachment_service import AgentAttachmentService
from .agent_workbench_event_queue import AgentWorkbenchEventQueueManager
from .agent_workbench_execution_service import AgentWorkbenchRunExecutionService
from .agent_workbench_run_service import AgentWorkbenchRunService
from internal.service.conversation.conversation_service import ConversationService

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
        # 权限校验已完成；释放其 Session，首批查询延后至 SSE 连接头发出后。
        self.db.session.close()

        def stream_events() -> Iterator[dict[str, Any] | None]:
            """每批读取后释放 Session；过滤事件同样推进内部扫描游标。"""
            cursor = stream_request.since
            heartbeat_at = time.monotonic() + 15
            with self.queue_manager.watch_events(context.conversation_id, context.invoke_from) as signal:
                while True:
                    version = signal.snapshot()
                    try:
                        batch = signal.read_events(
                            cursor,
                            recover=lambda: self.run_service.recover_stale_runs(conversation_id=context.conversation_id),
                            load=lambda: self.run_service.get_reconnect_events(
                                conversation_id=context.conversation_id,
                                invoke_from=context.invoke_from,
                                since=cursor,
                            ),
                        )
                    finally:
                        self.db.session.close()
                    for event in batch.events:
                        if stream_request.matches(event):
                            yield event.model_dump(mode="json")
                    cursor = batch.next_sequence
                    if time.monotonic() >= heartbeat_at:
                        yield None
                        heartbeat_at = time.monotonic() + 15
                    if not batch.has_more:
                        signal.wait(version, timeout=0.5)

        return stream_events()

    def get_state(
        self,
        *,
        context: AgentInvocationContext,
        view: WorkbenchStateView = "full",
    ) -> WorkbenchStateResp:
        """使用入口已校验的上下文读取工作台状态。"""
        self.run_service.recover_stale_runs(conversation_id=context.conversation_id)
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
        deferred_ids = {
            run.id for run in persisted_runs
            if view == "compact" and run.status in WORKBENCH_TERMINAL_STATUSES
        }
        events_by_run = self.run_service.list_events_by_run(
            [run.id for run in persisted_runs if run.id not in deferred_ids]
        )
        if deferred_ids:
            events_by_run.update(self.run_service.list_context_events_by_run(list(deferred_ids)))
        if view == "compact":
            values["deferred_run_ids"] = [str(run.id) for run in persisted_runs if run.id in deferred_ids]
        values["workbench_events"] = [
            event.model_dump(mode="json")
            for event in project_run_history(persisted_runs, events_by_run)
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
            tasks=[
                {"id": item["interrupt_id"], "name": "ask_user", "interrupts": [
                    {"id": item["interrupt_id"], "value": item["payload"], "ns": item.get("namespace", [])}
                ]}
                for item in values.get("pending_inputs", [])
            ] if status == "waiting_input" else [],
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
        row = self.db.session.query(AgentRun.app_id, AgentRun.conversation_id).filter(AgentRun.id == run_id).one_or_none()
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
        assembly = self.execution_service.agent_assembly_service
        try:
            raw_selection = saved.get("model_selection")
            selection = WorkbenchModelSelection.model_validate(raw_selection) if raw_selection else None
            settings = assembly.get_workbench_model_settings(context, selection)
        except (NotFoundException, ValidateErrorException, ValueError):
            # 已停用的历史模型不能继续使用，回退到当前应用的授权配置。
            try:
                settings = assembly.get_workbench_model_settings(context)
            except (NotFoundException, ValidateErrorException, ValueError):
                return WorkbenchModelSettingsResp()
            return settings
        effort = saved.get("reasoning_effort")
        if effort in settings.reasoning_levels:
            settings = settings.model_copy(update={"selected_reasoning_effort": effort})
        return settings

    def cancel_run(
        self,
        *,
        context: AgentInvocationContext,
        run_id: UUID,
    ) -> WorkbenchCancelResp:
        """使用入口已校验的上下文停止指定 Agent Run。"""
        # 1.校验目标 Run 是否属于本次请求的会话和入口。
        row = self.run_service.get_run(run_id, recover=False)
        if (
            row is None
            or row.app_id != context.app_id
            or row.conversation_id != context.conversation_id
            or row.invoke_from != context.invoke_from.value
        ):
            raise NotFoundException("Agent Run 不存在或不属于当前会话")
        # 归属确认后才允许恢复失联状态；未授权请求不能触发写入。
        row = self.run_service.get_run(run_id, recover=True)
        if row is None:
            raise NotFoundException("Agent Run 不存在")
        # 2.先提交数据库取消状态，再通知本地执行线程。
        cancel_requested = False
        if (
            row.status not in WORKBENCH_TERMINAL_STATUSES
            and self.run_service.request_cancel(row)
        ):
            cancel_requested = True
            self.queue_manager.notify_events(context.conversation_id, context.invoke_from)
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
                if cancel_requested and row.status != "cancelled"
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

    def start_run(
        self,
        *,
        context: AgentInvocationContext,
        run_input: WorkbenchRunInput | WorkbenchInputRespondReq | WorkbenchInputReopenReq,
        command_id: int | None = None,
        user_input_enabled: bool = True,
    ) -> WorkbenchCommandSuccessResp | WorkbenchCommandErrorResp:
        """锁定会话后完成去重和创建，提交成功才注册并启动后台执行。"""
        if isinstance(run_input, WorkbenchInputRespondReq):
            return self.respond_input(context=context, response=run_input, command_id=command_id)
        if isinstance(run_input, WorkbenchInputReopenReq):
            return self.reopen_input(context=context, request=run_input, command_id=command_id)
        conversation_id = context.conversation_id
        self.run_service.recover_stale_runs(conversation_id=conversation_id)

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
                    AgentRun.status.in_((*WORKBENCH_ACTIVE_STATUSES, "waiting_input")),
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
                        else "请先完成当前表单" if active_run is not None and active_run.status == "waiting_input"
                        else "当前任务已有正在执行的 Agent"
                    ),
                )

            previous = (
                self.db.session.query(AgentRun).filter(AgentRun.conversation_id == conversation_id)
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).first()
            )
            if previous is not None and previous.status == "cancelled" and (previous.state_snapshot or {}).get("_interaction", {}).get("cancelled_input"):
                return WorkbenchCommandErrorResp(
                    id=command_id,
                    message="此任务已在表单处停止，请点击重新填写后继续，或创建新会话"
                    if (previous.state_snapshot or {}).get("input_recovery")
                    else "此任务已在表单处停止，请创建新会话",
                )

            # 4.解析附件并创建业务消息，与 Run 一起提交或回滚。
            self.execution_service.agent_assembly_service.validate_workbench_reasoning(context, run_input.reasoning_effort, run_input.model_selection)
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
                planning_enabled=run_input.planning_enabled,
                selected_skills=run_input.selected_skills,
                reasoning_effort=run_input.reasoning_effort,
                model_selection=run_input.model_selection,
                input_message_id=run_input.input_message_id,
                user_input_enabled=user_input_enabled,
            )
            if user_input_enabled:
                invocation = invocation.model_copy(update={
                    "definition_fingerprint": self.execution_service.agent_assembly_service.get_definition_fingerprint(invocation),
                })
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
        self._launch_run(run)
        return WorkbenchCommandSuccessResp.create(
            command_id=command_id,
            run_id=run.run_id,
            applied_through_sequence=sequence,
        )

    def respond_input(
        self,
        *,
        context: AgentInvocationContext,
        response: WorkbenchInputRespondReq,
        command_id: int | None = None,
    ) -> WorkbenchCommandSuccessResp | WorkbenchCommandErrorResp:
        """锁定会话与暂停 Run，验证答案并原子认领唯一恢复执行权。"""
        with self.db.auto_commit():
            conversation = (
                self.db.session.query(Conversation.id)
                .filter(Conversation.id == context.conversation_id,
                        Conversation.app_id == context.app_id, Conversation.is_deleted.is_(False))
                .with_for_update().one_or_none()
            )
            if conversation is None:
                raise NotFoundException("会话不存在或已删除")
            row = (
                self.db.session.query(AgentRun).populate_existing()
                .filter(AgentRun.conversation_id == context.conversation_id,
                        AgentRun.app_id == context.app_id, AgentRun.invoke_from == context.invoke_from.value)
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                .with_for_update().first()
            )
            if row is None:
                raise NotFoundException("待回答任务不存在")
            snapshot = dict(row.state_snapshot or {})
            interaction = dict(snapshot.get("_interaction") or {})
            answers = {item.interrupt_id: item.response for item in response.responses}
            submissions = list(interaction.get("submissions") or [])
            for submitted in submissions:
                if set(submitted["answers"]) == set(answers):
                    if submitted["answers"] != answers:
                        raise ValidateErrorException("该表单已提交，不能重复修改")
                    return WorkbenchCommandSuccessResp.create(
                        command_id=command_id, run_id=row.id,
                        applied_through_sequence=submitted["sequence"],
                    )
            if row.status != "waiting_input":
                raise ValidateErrorException("当前任务没有等待填写的表单")
            pending = [PendingUserInput.model_validate(item) for item in snapshot.get("pending_inputs", [])]
            if not pending or set(answers) != {item.interrupt_id for item in pending}:
                raise ValidateErrorException("表单已失效，请刷新后填写所有待回答问题")
            validated = {}
            for item in pending:
                try:
                    validated[item.interrupt_id] = item.payload.validate_answers(answers[item.interrupt_id])
                except ValueError as exc:
                    field_name = getattr(exc, "field_name", None)
                    if field_name is None:
                        raise ValidateErrorException(str(exc)) from exc
                    return WorkbenchCommandErrorResp(
                        id=command_id, message=str(exc),
                        field_errors={item.interrupt_id: {field_name: str(exc)}},
                    )
            local_run = self.queue_manager.get(context.conversation_id)
            if local_run is not None and not local_run.done.is_set():
                return WorkbenchCommandErrorResp(id=command_id, message="任务正在保存，请稍后重试")
            invocation = AgentInvocation.model_validate(interaction.get("invocation"))
            # 恢复身份始终取自本次鉴权，保留原始附件与 Skill 选择。
            invocation = invocation.model_copy(update=context.model_dump())
            if invocation.definition_fingerprint != self.execution_service.agent_assembly_service.get_definition_fingerprint(invocation):
                raise ValidateErrorException("应用配置已变更，请恢复原配置后继续填写，或创建新会话")
            sequence = int(row.last_event_sequence or 0)
            run = WorkbenchRun(
                run_id=row.id, conversation_id=context.conversation_id, spec=invocation,
                initial_event_sequence=sequence, last_event_sequence=sequence, resume=validated,
            )
            history = list(snapshot.get("input_history") or [])
            history.extend({**item.model_dump(mode="json"), "answers": validated[item.interrupt_id]} for item in pending)
            submissions.append({"answers": deepcopy(answers), "sequence": sequence})
            row.state_snapshot = {
                **snapshot, "pending_inputs": [], "input_history": history, "input_recovery": None,
                "_interaction": {**interaction, "submissions": submissions},
            }
            row.status = "queued"
            row.executor_id = run.executor_id
            row.heartbeat_at = datetime.now(UTC).replace(tzinfo=None)
            row.finished_at = None
        self._launch_run(run)
        return WorkbenchCommandSuccessResp.create(
            command_id=command_id, run_id=run.run_id, applied_through_sequence=sequence,
        )

    def reopen_input(
        self, *, context: AgentInvocationContext, request: WorkbenchInputReopenReq,
        command_id: int | None = None,
    ) -> WorkbenchCommandSuccessResp | WorkbenchCommandErrorResp:
        """原子恢复最近一次取消的表单；重新填写前不启动图或获取执行租约。"""
        with self.db.auto_commit():
            conversation = (
                self.db.session.query(Conversation.id)
                .filter(Conversation.id == context.conversation_id,
                        Conversation.app_id == context.app_id, Conversation.is_deleted.is_(False))
                .with_for_update().one_or_none()
            )
            if conversation is None:
                raise NotFoundException("会话不存在或已删除")
            row = (
                self.db.session.query(AgentRun).populate_existing()
                .filter(AgentRun.conversation_id == context.conversation_id)
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).with_for_update().first()
            )
            if (row is None or row.id != request.run_id or row.app_id != context.app_id
                    or row.invoke_from != context.invoke_from.value):
                raise ValidateErrorException("仅支持恢复当前会话最新停止的表单，请刷新后重试")
            snapshot = dict(row.state_snapshot or {})
            interaction = dict(snapshot.get("_interaction") or {})
            if interaction.get("input_cancellation_sequence") != request.cancellation_sequence:
                raise ValidateErrorException("任务状态已更新，请刷新后重新操作")
            if interaction.get("last_reopened_cancellation_sequence") == request.cancellation_sequence:
                return WorkbenchCommandSuccessResp.create(
                    command_id=command_id, run_id=row.id,
                    applied_through_sequence=interaction["reopened_sequence"],
                )
            if row.status != "cancelled" or not interaction.get("cancelled_input"):
                raise ValidateErrorException("当前任务没有可恢复的表单")
            local_run = self.queue_manager.get(context.conversation_id)
            if local_run is not None and not local_run.done.is_set():
                return WorkbenchCommandErrorResp(id=command_id, message="任务正在停止，请稍后重试")
            pending = [PendingUserInput.model_validate(item).model_dump(mode="json")
                       for item in interaction.get("cancelled_inputs", [])]
            if not pending:
                raise ValidateErrorException("此任务没有保留待填写内容，请创建新会话")
            invocation = AgentInvocation.model_validate(interaction.get("invocation"))
            invocation = invocation.model_copy(update=context.model_dump())
            if invocation.definition_fingerprint != self.execution_service.agent_assembly_service.get_definition_fingerprint(invocation):
                raise ValidateErrorException("应用配置已变更，请恢复原配置后重新填写，或创建新会话")
            snapshot.update(pending_inputs=pending, input_recovery=None)
            projector = AgentProtocolEventProjector(row.id, start_sequence=int(row.last_event_sequence or 0))
            events = [projector.generated("values", project_snapshot_values(snapshot))]
            events.extend(projector.generated("input.requested", {
                "interrupt_id": item["interrupt_id"], "payload": item["payload"],
            }) for item in pending)
            events.append(projector.generated("lifecycle", {
                "event": "interrupted", "status": "waiting_input", "reason": "input_reopened",
                "message_id": str(row.message_id), "input_message_id": row.input_message_id,
            }))
            for raw in events:
                event = WorkbenchEvent.model_validate(raw)
                self.db.session.add(AgentRunEvent(
                    run_id=row.id, sequence=event.seq, payload=event.model_dump(mode="json"),
                ))
            sequence = projector.last_event_sequence
            snapshot["_interaction"] = {
                **interaction, "cancelled_input": False, "cancelled_inputs": [],
                "last_reopened_cancellation_sequence": request.cancellation_sequence,
                "reopened_sequence": sequence,
            }
            row.state_snapshot = snapshot
            row.status = "waiting_input"
            row.finished_at = None
            row.executor_id = None
            row.last_event_sequence = sequence
            message = self.db.session.get(Message, row.message_id)
            if message is not None:
                message.status = "normal"
        self.queue_manager.notify_events(context.conversation_id, context.invoke_from)
        return WorkbenchCommandSuccessResp.create(
            command_id=command_id, run_id=request.run_id, applied_through_sequence=sequence,
        )

    def _launch_run(self, run: WorkbenchRun) -> None:
        """只在事务提交后启动线程，启动失败由原有失败路径收敛。"""
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

    def _execute_background_run(self, flask_app: Flask, run: WorkbenchRun) -> None:
        """后台线程使用独立应用上下文和 Session，退出时兜底收敛失败状态。"""
        with flask_app.app_context():
            lease_done = threading.Event()
            lease_monitor = threading.Thread(
                target=self._watch_run_lease,
                args=(flask_app, run, lease_done),
                name=f"agent-lease-{run.run_id}",
                daemon=True,
            )
            try:
                if not self.run_service.renew_lease(run):
                    raise RuntimeError("Agent Run 执行权已失效")
                lease_monitor.start()
                self.execution_service.execute(run)
            except Exception as exc:
                self._fail_background_run(run, exc)
            finally:
                lease_done.set()
                if lease_monitor.ident is not None:
                    lease_monitor.join(timeout=1)
                self.db.session.remove()

    def _watch_run_lease(
        self, flask_app: Flask, run: WorkbenchRun, done: threading.Event
    ) -> None:
        """从装配开始独立续租，模型无输出或工具耗时不会被误判为进程退出。"""
        last_renewed = time.monotonic()
        with flask_app.app_context():
            while not done.wait(10):
                try:
                    if not self.run_service.renew_lease(run):
                        run.cancel_requested.set()
                        return
                    last_renewed = time.monotonic()
                except Exception:
                    logger.exception("Agent Run 心跳更新失败 run_id=%s", run.run_id)
                    if time.monotonic() - last_renewed >= self.run_service.lease_seconds:
                        run.cancel_requested.set()
                        return
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
                self.queue_manager.notify_events(run.conversation_id, run.spec.invoke_from)
        except Exception:
            logger.exception("保存 Agent Run 后台失败状态失败")
        finally:
            self.queue_manager.finish(run)
        logger.error(
            "Agent 工作台后台执行失败",
            exc_info=(type(exc), exc, exc.__traceback__),
        )


__all__ = ["AgentWorkbenchService"]
