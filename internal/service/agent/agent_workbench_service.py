"""Agent 运行命令：启动、取消、提交答案与重新填写。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from injector import inject
from internal.entity import (
    WORKBENCH_ACTIVE_STATUSES,
    WORKBENCH_TERMINAL_STATUSES,
    WorkbenchRun,
    WorkbenchRunInput,
    WorkbenchRunStatus,
)
from internal.entity.agent_run_interaction import (
    AgentRunInteraction,
    InvalidInputAnswer,
    RunAccepted,
    RunRejected,
    RunCancellation,
)
from internal.exception import NotFoundException, ValidateErrorException
from internal.model import AgentRun
from internal.schema.agent.agent_attachment_schema import dump_agent_attachments
from internal.entity.agent_invocation import (
    AgentInvocation,
    AgentInvocationContext,
)
from internal.service.conversation.conversation_service import ConversationService
from pkg.sqlalchemy import SQLAlchemy
from .agent_definition_service import AgentDefinitionService
from .agent_model_service import AgentModelService
from .agent_attachment_service import AgentAttachmentService
from .agent_workbench_event_queue import AgentWorkbenchEventQueueManager
from .agent_workbench_execution_service import AgentWorkbenchRunExecutionService
from .agent_workbench_run_service import AgentWorkbenchRunService


@inject
@dataclass
class AgentWorkbenchService:
    """编排业务规则与事务；查询、协议响应及后台执行由各自模块负责。"""

    db: SQLAlchemy
    run_service: AgentWorkbenchRunService
    queue_manager: AgentWorkbenchEventQueueManager
    execution_service: AgentWorkbenchRunExecutionService
    definition_service: AgentDefinitionService
    model_service: AgentModelService
    attachment_service: AgentAttachmentService
    conversation_service: ConversationService

    def start_run(
        self,
        *,
        context: AgentInvocationContext,
        run_input: WorkbenchRunInput,
        user_input_enabled: bool = True,
    ) -> RunAccepted | RunRejected:
        """锁定会话后完成去重和创建，提交成功才注册并启动后台执行。"""
        conversation_id = context.conversation_id
        self.run_service.recover_stale_runs(conversation_id=conversation_id)

        with self.db.auto_commit():
            self.run_service.lock_conversation(context)
            duplicate_response = self._check_duplicate_start(
                context=context,
                run_input=run_input,
            )
            if duplicate_response is not None:
                return duplicate_response
            conflict_response = self._check_start_conflict(
                context=context,
            )
            if conflict_response is not None:
                return conflict_response
            run, sequence = self._create_run(
                context=context,
                run_input=run_input,
                user_input_enabled=user_input_enabled,
            )

        # 提交成功后登记本地 Run，并在独立 Flask 上下文中启动执行。
        self.execution_service.launch(run)
        return RunAccepted(
            run_id=run.run_id,
            sequence=sequence,
        )

    def respond_input(
        self,
        *,
        context: AgentInvocationContext,
        answers: Mapping[str, Any],
    ) -> RunAccepted | RunRejected:
        """锁定会话与暂停 Run，验证答案并原子认领唯一恢复执行权。"""
        with self.db.auto_commit():
            # 1.先锁会话再锁最新 Run，与启动和重新填写保持同一锁顺序。
            self.run_service.lock_conversation(context)
            row = (
                self.db.session.query(AgentRun)
                .populate_existing()
                .filter(
                    AgentRun.conversation_id == context.conversation_id,
                    AgentRun.app_id == context.app_id,
                    AgentRun.invoke_from == context.invoke_from.value,
                )
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                .with_for_update()
                .first()
            )
            if row is None:
                raise NotFoundException("待回答任务不存在")
            interaction = AgentRunInteraction(row.status, row.state_snapshot)
            try:
                # 重试必须先于状态判断，已恢复的任务也返回首次提交结果。
                sequence = interaction.submitted_sequence(answers)
                if sequence is not None:
                    return RunAccepted(row.id, sequence)
                validated = interaction.validate_answers(answers)
            except InvalidInputAnswer as exc:
                return RunRejected(str(exc), {exc.interrupt_id: {exc.field_name: str(exc)}})
            except ValueError as exc:
                raise ValidateErrorException(str(exc)) from exc
            # 2.前一执行实例释放资源、配置指纹一致后，才允许认领恢复执行权。
            local_run = self.queue_manager.get(context.conversation_id)
            if local_run is not None and not local_run.done.is_set():
                return RunRejected(message="任务正在保存，请稍后重试")
            invocation = self._restore_input_invocation(
                context, interaction,
                changed_message="应用配置已变更，请恢复原配置后继续填写，或创建新会话",
            )
            sequence = int(row.last_event_sequence or 0)
            run = WorkbenchRun(
                run_id=row.id, conversation_id=context.conversation_id, spec=invocation,
                initial_event_sequence=sequence, last_event_sequence=sequence, resume=validated,
            )
            self.run_service.queue_resumed_run(
                row, run, interaction.after_submission(answers, validated, sequence),
            )
        # 3.提交成功后才启动执行，避免后台读取到尚未保存的回答。
        self.execution_service.launch(run)
        return RunAccepted(
            run_id=run.run_id, sequence=sequence,
        )

    def reopen_input(
        self, *, context: AgentInvocationContext, run_id: UUID, cancellation_sequence: int,
    ) -> RunAccepted | RunRejected:
        """原子恢复最近一次取消的表单；重新填写前不启动图或获取执行租约。"""
        with self.db.auto_commit():
            # 1.锁定会话和最新 Run，仅允许重新填写当前入口最近停止的表单。
            self.run_service.lock_conversation(context)
            row = (
                self.db.session.query(AgentRun)
                .populate_existing()
                .filter(AgentRun.conversation_id == context.conversation_id)
                .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
                .with_for_update()
                .first()
            )
            if row is None:
                raise ValidateErrorException("仅支持恢复当前会话最新停止的表单，请刷新后重试")
            same_run = row.id == run_id and row.app_id == context.app_id
            same_source = row.invoke_from == context.invoke_from.value
            if not same_run or not same_source:
                raise ValidateErrorException("仅支持恢复当前会话最新停止的表单，请刷新后重试")
            # 2.先核对取消序号和重复请求，再判断当前状态，保证重试仍返回相同结果。
            interaction = AgentRunInteraction(row.status, row.state_snapshot)
            try:
                sequence = interaction.reopened_sequence(cancellation_sequence)
            except ValueError as exc:
                raise ValidateErrorException(str(exc)) from exc
            if sequence is not None:
                return RunAccepted(row.id, sequence)
            local_run = self.queue_manager.get(context.conversation_id)
            if local_run is not None and not local_run.done.is_set():
                return RunRejected(message="任务正在停止，请稍后重试")
            # 3.恢复保留的表单与可信身份，拒绝用变更后的应用配置继续旧任务。
            try:
                snapshot = interaction.restored_snapshot()
            except ValueError as exc:
                raise ValidateErrorException(str(exc)) from exc
            self._restore_input_invocation(
                context, interaction,
                changed_message="应用配置已变更，请恢复原配置后重新填写，或创建新会话",
            )
            sequence = self.run_service.reopen_pending_run(
                row, interaction, snapshot, cancellation_sequence,
            )
        # 4.事务提交后再唤醒订阅者，确保它能读到全部表单事件。
        self.queue_manager.notify_events(context.conversation_id, context.invoke_from)
        return RunAccepted(
            run_id=run_id, sequence=sequence,
        )

    def cancel_run(
        self,
        *,
        context: AgentInvocationContext,
        run_id: UUID,
    ) -> RunCancellation:
        """使用入口已校验的上下文停止指定 Agent Run。"""
        # 1.校验目标 Run 是否属于本次请求的会话和入口。
        self.run_service.get_context_run(context, run_id)
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
        response_status = str(row.status)
        if cancel_requested and row.status != "cancelled":
            response_status = WorkbenchRunStatus.CANCELLING.value

        return RunCancellation(
            cancel_requested=cancel_requested,
            run_id=row.id,
            status=response_status,
        )

    def _restore_input_invocation(
        self,
        context: AgentInvocationContext,
        interaction: AgentRunInteraction,
        *,
        changed_message: str,
    ) -> AgentInvocation:
        """恢复原始输入但采用当前鉴权身份，图配置变化时拒绝继续旧任务。"""
        invocation = AgentInvocation.model_validate(interaction.invocation)
        invocation = invocation.model_copy(update=context.model_dump())
        if invocation.definition_fingerprint != self.definition_service.get_definition_fingerprint(invocation):
            raise ValidateErrorException(changed_message)
        return invocation

    def _check_duplicate_start(
        self,
        *,
        context: AgentInvocationContext,
        run_input: WorkbenchRunInput,
    ) -> RunAccepted | RunRejected | None:
        """处理同一输入的重复提交，保证重试返回同一个 Run。"""
        duplicate_run = self.run_service.get_run_by_input_message(
            conversation_id=context.conversation_id,
            input_message_id=run_input.input_message_id,
        )
        if duplicate_run is None:
            return None
        if duplicate_run.invoke_from != context.invoke_from.value:
            return RunRejected(
                message="输入消息已用于其他调用入口",
            )

        local_run = self.queue_manager.get(context.conversation_id)
        applied_sequence = self.run_service.get_initial_event_sequence(duplicate_run)
        if local_run is not None and local_run.run_id == duplicate_run.id:
            applied_sequence = local_run.initial_event_sequence
        return RunAccepted(
            run_id=duplicate_run.id,
            sequence=applied_sequence,
        )

    def _check_start_conflict(
        self,
        *,
        context: AgentInvocationContext,
    ) -> RunRejected | None:
        """判断会话是否仍被活动、停止中或待输入的 Run 占用。"""
        active_run = (
            self.db.session.query(AgentRun)
            .filter(
                AgentRun.conversation_id == context.conversation_id,
                AgentRun.status.in_((*WORKBENCH_ACTIVE_STATUSES, "waiting_input")),
            )
            .first()
        )
        local_run = self.queue_manager.get(context.conversation_id)
        has_active_database_run = active_run is not None
        has_active_local_run = local_run is not None and not local_run.done.is_set()
        if has_active_database_run or has_active_local_run:
            stopping = local_run is not None and local_run.cancel_requested.is_set()
            waiting_input = False
            if active_run is not None:
                stopping = stopping or active_run.status == WorkbenchRunStatus.CANCELLING.value
                waiting_input = active_run.status == "waiting_input"
            if stopping:
                reason = "当前任务正在停止，请稍后重试"
            elif waiting_input:
                reason = "请先完成当前表单"
            else:
                reason = "当前任务已有正在执行的 Agent"
            return RunRejected(message=reason)

        previous = (
            self.db.session.query(AgentRun)
            .filter(AgentRun.conversation_id == context.conversation_id)
            .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
            .first()
        )
        if previous is None or previous.status != "cancelled":
            return None
        reason = AgentRunInteraction(previous.status, previous.state_snapshot).start_conflict()
        return RunRejected(reason) if reason else None

    def _create_run(
        self,
        *,
        context: AgentInvocationContext,
        run_input: WorkbenchRunInput,
        user_input_enabled: bool,
    ) -> tuple[WorkbenchRun, int]:
        """在当前事务中创建消息、Invocation 和本地 Run。"""
        self.model_service.validate_workbench_reasoning(
            context,
            run_input.reasoning_effort,
            run_input.model_selection,
        )
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
            model_selection_explicit=run_input.model_selection is not None,
            input_message_id=run_input.input_message_id,
            user_input_enabled=user_input_enabled,
        )
        if user_input_enabled:
            fingerprint = self.definition_service.get_definition_fingerprint(invocation)
            invocation = invocation.model_copy(
                update={"definition_fingerprint": fingerprint}
            )
        sequence = self.run_service.get_last_event_sequence(
            conversation_id=context.conversation_id,
            invoke_from=context.invoke_from,
            close_session=False,
        )
        run = WorkbenchRun(
            run_id=uuid4(),
            conversation_id=context.conversation_id,
            spec=invocation,
            initial_event_sequence=sequence,
            last_event_sequence=sequence,
        )
        self.run_service.add_run(run)
        return run, sequence


__all__ = ["AgentWorkbenchService"]
