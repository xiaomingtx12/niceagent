"""Agent 执行：启动、重启、提交答案、取消，以及执行持久化与后台运行。

公开方法在请求线程执行，只编排事务和业务规则；``_submit_execution`` 及其后的
下划线方法在后台线程执行，写入资格由 Run 的 executor_id 校验保证。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Collection, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from flask import Flask, current_app
from injector import inject
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import load_only

from config import Config
from internal.core.agent_v2 import (
    AGENT_PERSISTENCE_ERRORS,
    AgentEvent,
    AgentExecutionResult,
    AgentExecutor,
    AgentMessageError,
    PendingUserInput,
    PlatformAgentRunObserver,
    get_postgres_pool_stats,
)
from internal.entity import (
    AGENT_EXECUTION_ACTIVE_STATUSES,
    AGENT_EXECUTION_OCCUPYING_STATUSES,
    AGENT_EXECUTION_TERMINAL_STATUSES,
    AgentExecutionHandle,
    AgentExecutionInput,
    AgentExecutionStatus,
)
from internal.entity.agent_invocation import AgentExecutionOptions, AgentInvocation
from internal.entity.agent_execution_interaction import (
    AgentExecutionInteraction,
    InvalidInputAnswer,
    ExecutionAccepted,
    ExecutionRejected,
)
from internal.entity.conversation_entity import InvokeFrom, MessageStatus
from internal.exception import (
    CustomException,
    FailException,
    NotFoundException,
    ValidateErrorException,
)
from internal.extension.logging_extension import logger
from internal.model import AgentExecution, AgentExecutionEvent, AgentExecutionSubmission, Conversation, ConversationTurn
from internal.schema.agent.agent_attachment_schema import dump_agent_attachments
from internal.schema.agent.agent_protocol_schema import (
    AgentProtocolCancelResp,
    AgentProtocolEvent,
    AgentProtocolRunMetadataResp,
)
from internal.service.agent_protocol.event_projector import AgentProtocolEventProjector
from internal.service.agent_protocol.state_projector import (
    adapt_graph_state,
    select_public_snapshot,
)
from internal.service.conversation.conversation_service import ConversationService
from pkg.response import HttpCode
from pkg.sqlalchemy import SQLAlchemy

from .agent_attachment_service import AgentAttachmentService
from .agent_definition_service import AgentDefinitionService
from .agent_factory import AgentFactory
from .agent_model_service import AgentModelService
from .agent_execution_runtime import AgentExecutionEventBuffer, AgentExecutionTracker


@inject
@dataclass
class AgentExecutionService:
    """Run 聚合的唯一入口：命令、持久化与后台执行编排都在本文件内闭合。"""

    db: SQLAlchemy
    tracker: AgentExecutionTracker
    factory: AgentFactory
    definition_service: AgentDefinitionService
    model_service: AgentModelService
    attachment_service: AgentAttachmentService
    conversation_service: ConversationService
    conf: Config
    _run_executor: ThreadPoolExecutor = field(init=False, repr=False)
    _run_slots: threading.BoundedSemaphore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """单例服务共享进程内执行池，配置在实例化时读取，避免早于 .env 加载。"""
        workers = self.conf.AGENT_RUN_MAX_WORKERS
        self._run_executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agent-run")
        self._run_slots = threading.BoundedSemaphore(workers)

    def start_run(
        self, *, account_id: UUID, app_id: UUID, conversation_id: UUID, created_by: UUID,
        end_user_id: UUID | None = None, invoke_from: InvokeFrom, run_input: AgentExecutionInput,
        user_input_enabled: bool = True,
    ) -> ExecutionAccepted | ExecutionRejected:
        """锁定会话后完成去重和创建，提交成功才注册并启动后台执行。"""
        # 1.先收敛本进程已不再持有的运行记录，避免进程残留的 running 长期占住会话。
        self._settle_orphan_runs(conversation_id=conversation_id)

        with self._claimed_run() as claimed:
            with self.db.auto_commit():
                # 2.锁定会话，让同一会话的启动检查与创建串行执行。
                self._lock_conversation(app_id=app_id, conversation_id=conversation_id)
                # 3.同一输入消息重复提交直接返回原 Run；会话被占用时返回可展示的冲突原因。
                duplicate_response = self._check_duplicate_start(
                    conversation_id=conversation_id, invoke_from=invoke_from,
                    run_input=run_input,
                )
                if duplicate_response is not None:
                    return duplicate_response
                conflict_response = self._check_start_conflict(conversation_id=conversation_id)
                if conflict_response is not None:
                    return conflict_response
                # 4.创建消息、调用输入和 Run 记录；执行标识在建行时即已分配。
                run, sequence = self._create_run(
                    account_id=account_id, app_id=app_id,
                    conversation_id=conversation_id, created_by=created_by,
                    end_user_id=end_user_id, invoke_from=invoke_from,
                    run_input=run_input, user_input_enabled=user_input_enabled,
                )
                # 提交前登记本地执行，否则同一时刻的其他请求会把这条新的运行中记录判为孤儿。
                self.tracker.register(run)
                claimed.append(run)

        # 5.提交成功后才投递执行，后台线程不会读到尚未提交的消息与 Run。
        self._submit_execution(run)
        return ExecutionAccepted(
            run_id=run.run_id,
            sequence=sequence,
        )

    def restart_run(
        self, *, account_id: UUID, app_id: UUID, conversation_id: UUID, created_by: UUID,
        end_user_id: UUID | None = None, invoke_from: InvokeFrom, run_input: AgentExecutionInput,
    ) -> ExecutionAccepted | ExecutionRejected:
        """内部 SDK 更新：先校验，再停止并等待前序执行释放，最后沿用正常启动事务。"""
        # 1.先校验新输入，任何失败都不能影响旧 Run。
        # 停止前验证参数和附件归属；等待后 start_run 再复核，避免使用失效授权。
        self.model_service.validate_reasoning_effort(
            account_id=account_id, app_id=app_id, created_by=created_by,
            invoke_from=invoke_from, effort=run_input.reasoning_effort,
            selection=run_input.model_selection,
        )
        self.attachment_service.resolve(run_input.file_ids, owner_id=account_id)
        # 2.读取当前入口最近一次 Run，只在本入口范围内替换，不跨入口停止别人的任务。
        previous = (
            self.db.session.query(AgentExecution)
            .populate_existing()
            .filter(
                AgentExecution.conversation_id == conversation_id,
                AgentExecution.invoke_from == invoke_from.value,
            )
            .order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc())
            .first()
        )
        if previous is not None:
            previous_id = previous.id
            # 3.前序任务未结束时先请求取消；已结束的直接进入收敛确认。
            if not previous.is_terminal:
                self.cancel_run(
                    app_id=app_id, conversation_id=conversation_id,
                    invoke_from=invoke_from, run_id=previous_id,
                )
            # 前序 Run 未收敛时不能创建新 Run：数据库约束同会话只有一个占用中的 Run。
            local = self.tracker.get(conversation_id)
            deadline = time.monotonic() + 10
            while True:
                # 4.取消请求与数据库终态都确认后，才允许创建新 Run。
                try:
                    status = self.get_run_status(previous_id)
                finally:
                    self.db.session.close()
                locally_released = local is None or local.run_id != previous_id or local.done.is_set()
                if status in AGENT_EXECUTION_TERMINAL_STATUSES and locally_released:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # 5.超时返回未启动，此时没有创建任何新记录，调用方可以安全重试。
                    return ExecutionRejected("前序任务尚未停止，本次更新未启动，请稍后重试")
                time.sleep(min(0.1, remaining))
        # 6.前序已收敛，沿用正常启动事务创建新 Run；更新场景不允许再进入表单填写。
        return self.start_run(
            account_id=account_id, app_id=app_id,
            conversation_id=conversation_id, created_by=created_by,
            end_user_id=end_user_id, invoke_from=invoke_from, run_input=run_input,
            user_input_enabled=False,
        )

    def submit_answers(
        self, *, account_id: UUID, app_id: UUID, conversation_id: UUID, created_by: UUID,
        end_user_id: UUID | None = None, invoke_from: InvokeFrom, answers: Mapping[str, Any],
    ) -> ExecutionAccepted | ExecutionRejected:
        """锁定会话与暂停 Run，验证答案并原子认领唯一恢复执行权。"""
        with self._claimed_run() as claimed:
            with self.db.auto_commit():
                # 1.先锁会话再锁最新 Run，与启动保持同一锁顺序，避免互相死锁。
                self._lock_conversation(app_id=app_id, conversation_id=conversation_id)
                # 已结束批次的迟到重试也返回原回执，不能误交给后来创建的 Run。
                batch_id = AgentExecutionInteraction.input_batch_id(answers)
                submission = (
                    self.db.session.query(AgentExecutionSubmission)
                    .join(AgentExecution, AgentExecution.id == AgentExecutionSubmission.execution_id)
                    .filter(
                        AgentExecution.conversation_id == conversation_id,
                        AgentExecution.app_id == app_id,
                        AgentExecution.invoke_from == invoke_from.value,
                        AgentExecutionSubmission.input_batch_id == batch_id,
                    )
                    .one_or_none()
                )
                if submission is not None:
                    if submission.answers != answers:
                        raise ValidateErrorException("该表单已提交，不能重复修改")
                    return ExecutionAccepted(submission.execution_id, submission.accepted_sequence)
                row = (
                    self.db.session.query(AgentExecution)
                    .populate_existing()
                    .filter(
                        AgentExecution.conversation_id == conversation_id,
                        AgentExecution.app_id == app_id,
                        AgentExecution.invoke_from == invoke_from.value,
                    )
                    .order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc())
                    .with_for_update()
                    .first()
                )
                if row is None:
                    raise NotFoundException("待回答任务不存在")
                interaction = AgentExecutionInteraction(row.status, row.pending_inputs, row.input_history)
                try:
                    # 2.新提交只能回答当前等待批次；终态和过期表单不能恢复执行。
                    validated = interaction.validate_answers(answers)
                except InvalidInputAnswer as exc:
                    return ExecutionRejected(str(exc), {exc.interrupt_id: {exc.field_name: str(exc)}})
                except ValueError as exc:
                    raise ValidateErrorException(str(exc)) from exc
                # 3.前一执行实例释放资源、配置指纹一致后，才允许认领恢复执行权。
                busy = self._check_local_idle(
                    conversation_id=conversation_id, busy_message="任务正在保存，请稍后重试",
                )
                if busy is not None:
                    return busy
                invocation = self._restore_input_invocation(
                    account_id=account_id, app_id=app_id,
                    conversation_id=conversation_id, created_by=created_by,
                    end_user_id=end_user_id, invoke_from=invoke_from,
                    execution=row,
                    changed_message='应用配置已变更，请恢复原配置后继续填写，或停止本轮后发送新消息',
                )
                sequence = int(row.last_event_sequence or 0)
                resumed = AgentExecutionHandle(
                    run_id=row.id, conversation_id=conversation_id, spec=invocation,
                    initial_event_sequence=sequence, last_event_sequence=sequence, resume=validated,
                )
                # 4.答案、问答历史和新执行标识在同一事务保存；中断 ID 集合标识批次，重试不会重复恢复。
                row.input_history = interaction.after_submission(validated)
                row.pending_inputs = []
                self.db.session.add(AgentExecutionSubmission(
                    execution_id=row.id, input_batch_id=batch_id,
                    answers=deepcopy(dict(answers)), accepted_sequence=sequence,
                ))
                row.status = AgentExecutionStatus.RUNNING.value
                row.finished_at = None
                self._acquire_execution(row, resumed)
                # 提交前登记本地执行，否则同一时刻的其他请求会把这条新的运行中记录判为孤儿。
                self.tracker.register(resumed)
                claimed.append(resumed)
        # 5.提交成功后才启动执行，避免后台读取到尚未保存的回答。
        self._submit_execution(resumed)
        return ExecutionAccepted(
            run_id=resumed.run_id, sequence=int(resumed.initial_event_sequence or 0),
        )

    def cancel_run(
        self, *, app_id: UUID, conversation_id: UUID, invoke_from: InvokeFrom, run_id: UUID,
    ) -> AgentProtocolCancelResp:
        """核对应用、会话和调用来源后停止指定 Agent Run。"""
        # 1.校验目标 Run 是否属于本次请求的会话和入口；此校验不能触发失联恢复写入。
        row = (
            self.db.session.query(AgentExecution)
            .filter(
                AgentExecution.id == run_id,
                AgentExecution.app_id == app_id,
                AgentExecution.conversation_id == conversation_id,
                AgentExecution.invoke_from == invoke_from.value,
            )
            .one_or_none()
        )
        if row is None:
            raise NotFoundException("Agent Run 不存在或不属于当前会话")
        # 归属确认后才允许收敛孤儿记录；未授权请求不能触发写入。
        # 收敛走独立事务提交，传入行对象随 Session 过期，后续访问自动刷新为最新状态。
        self._settle_orphan_runs(run_id=run_id)
        # 2.先提交数据库取消请求，再通知本地执行线程。
        cancel_requested = False
        if (
            not row.is_terminal
            and self._request_cancel(row)
        ):
            cancel_requested = True
            # 订阅者先读到取消后的状态，本进程执行线程再收到取消信号。
            self.tracker.request_cancel(conversation_id, row.id)

        # 3.原样返回真实状态；等待表单的任务已直接取消，执行中的任务仍是 running。
        return AgentProtocolCancelResp(
            cancel_requested=cancel_requested,
            run_id=row.id,
            status=str(row.status),
        )

    def _restore_input_invocation(
        self, *, account_id: UUID, app_id: UUID, conversation_id: UUID, created_by: UUID,
        end_user_id: UUID | None = None, invoke_from: InvokeFrom, execution: AgentExecution,
        changed_message: str,
    ) -> AgentInvocation:
        """恢复原始输入但采用当前鉴权身份，图配置变化时拒绝继续旧任务。"""
        turn = self.db.session.get(ConversationTurn, execution.turn_id)
        if (turn is None or turn.is_deleted or turn.app_id != app_id
                or turn.conversation_id != conversation_id or turn.invoke_from != invoke_from.value):
            raise NotFoundException("待恢复问答不存在或归属不一致")
        # 问答保存的是公开附件元数据，读取内容需要按文件 ID 重新授权并解析存储键。
        attachments = self.attachment_service.resolve(
            tuple(UUID(str(item["id"])) for item in (turn.attachments or [])),
            owner_id=account_id,
        )
        invocation = AgentInvocation.model_validate({
            **(execution.execution_options or {}),
            "account_id": account_id, "app_id": app_id, "conversation_id": conversation_id,
            "created_by": created_by, "end_user_id": end_user_id, "invoke_from": invoke_from,
            "turn_id": turn.id, "query": turn.query,
            "image_urls": turn.image_urls, "attachments": attachments,
            "input_message_id": execution.input_message_id,
        })
        fingerprint = self.definition_service.load(
            account_id=account_id, app_id=app_id, invoke_from=invoke_from,
        ).fingerprint()
        if invocation.definition_fingerprint != fingerprint:
            raise ValidateErrorException(changed_message)
        return invocation

    def _check_local_idle(
        self, *, conversation_id: UUID, busy_message: str,
    ) -> ExecutionRejected | None:
        """本地有未结束的执行段就拒绝；返回 None 表示可继续，不触碰数据库。"""
        local_run = self.tracker.get(conversation_id)
        if local_run is not None and not local_run.done.is_set():
            return ExecutionRejected(message=busy_message)
        return None

    @contextmanager
    def _claimed_run(self) -> Iterator[list[AgentExecutionHandle]]:
        """提交前已登记的 Run：异常时反登记；提交后由调用方启动执行。"""
        claimed: list[AgentExecutionHandle] = []
        try:
            yield claimed
        except Exception:
            for run in claimed:
                self.tracker.finish(run)
            raise

    def _check_duplicate_start(
        self, *, conversation_id: UUID, invoke_from: InvokeFrom, run_input: AgentExecutionInput,
    ) -> ExecutionAccepted | ExecutionRejected | None:
        """处理同一输入的重复提交，保证重试返回同一个 Run。"""
        # 1.按输入消息查幂等 Run；没有幂等标识说明本次调用不承诺去重。
        if run_input.input_message_id is None:
            return None
        duplicate_run = (
            self.db.session.query(AgentExecution)
            .filter(
                AgentExecution.conversation_id == conversation_id,
                AgentExecution.input_message_id == run_input.input_message_id,
            )
            .one_or_none()
        )
        if duplicate_run is None:
            return None
        # 2.输入消息已被其他入口占用时不能复用，两个入口的事件流和权限互不相通。
        if duplicate_run.invoke_from != invoke_from.value:
            return ExecutionRejected(
                message="输入消息已用于其他调用入口",
            )

        # 同一标识只能重试同一份输入，不能把改过的消息静默当成旧请求接受。
        turn = self.db.session.get(ConversationTurn, duplicate_run.turn_id)
        if turn is None:
            raise NotFoundException("原始问答不存在")
        options = AgentExecutionOptions.model_validate(duplicate_run.execution_options or {})
        original_input = AgentExecutionInput(
            query=turn.query,
            image_urls=tuple(turn.image_urls or []),
            file_ids=tuple(UUID(str(item["id"])) for item in (turn.attachments or [])),
            planning_enabled=options.planning_enabled,
            selected_skills=options.selected_skills,
            reasoning_effort=options.reasoning_effort,
            model_selection=options.model_selection,
            input_message_id=duplicate_run.input_message_id,
        )
        if original_input != run_input:
            return ExecutionRejected(message="消息 id 已用于不同输入；发送新消息时请使用新 id")

        # 3.重放起点取本 Run 最早的事件序号，不能把终态游标当作起点；
        # 本进程仍持有该执行时以内存游标为准，避免读到尚未提交的事件序号。
        local_run = self.tracker.get(conversation_id)
        if local_run is not None and local_run.run_id == duplicate_run.id:
            applied_sequence = local_run.initial_event_sequence
        else:
            first_sequence = (
                self.db.session.query(func.min(AgentExecutionEvent.sequence))
                .filter(AgentExecutionEvent.execution_id == duplicate_run.id)
                .scalar()
            )
            applied_sequence = (
                int(first_sequence) - 1
                if first_sequence is not None
                else int(duplicate_run.last_event_sequence or 0)
            )
        return ExecutionAccepted(
            run_id=duplicate_run.id,
            sequence=applied_sequence,
        )

    def _check_start_conflict(
        self, *, conversation_id: UUID,
    ) -> ExecutionRejected | None:
        """判断会话是否仍被活动、停止中或待输入的 Run 占用。"""
        # 1.数据库占用（活动/待填）与本地未结束执行任一存在都不能新建 Run。
        active_run = (
            self.db.session.query(AgentExecution.status, AgentExecution.cancel_requested_at)
            .filter(
                AgentExecution.conversation_id == conversation_id,
                AgentExecution.status.in_(AGENT_EXECUTION_OCCUPYING_STATUSES),
            )
            .first()
        )
        local_run = self.tracker.get(conversation_id)
        has_active_database_run = active_run is not None
        has_active_local_run = local_run is not None and not local_run.done.is_set()
        if has_active_database_run or has_active_local_run:
            # 2.按优先级挑一条可展示的原因：正在停止 > 待填表单 > 正在执行。
            stopping = local_run is not None and local_run.cancel_requested.is_set()
            waiting_input = False
            if active_run is not None:
                waiting_input = active_run.status == AgentExecutionStatus.WAITING_INPUT.value
                # 等待表单优先提示；取消标记只在执行中才代表“正在停止”。
                stopping = stopping or (
                    not waiting_input and active_run.cancel_requested_at is not None
                )
            if stopping:
                reason = "当前任务正在停止，请稍后重试"
            elif waiting_input:
                reason = "请先完成当前表单"
            else:
                reason = "当前任务已有正在执行的 Agent"
            return ExecutionRejected(message=reason)

        return None

    def _create_run(
        self, *, account_id: UUID, app_id: UUID, conversation_id: UUID, created_by: UUID,
        end_user_id: UUID | None = None, invoke_from: InvokeFrom, run_input: AgentExecutionInput,
        user_input_enabled: bool,
    ) -> tuple[AgentExecutionHandle, int]:
        """在当前事务中创建消息、Invocation 和本地 Run。"""
        # 校验与恢复指纹共用本次读取的应用定义，避免持有会话锁时重复装配配置。
        definition = self.definition_service.load(
            account_id=account_id, app_id=app_id, invoke_from=invoke_from,
        ) if user_input_enabled else None
        # 1.先校验模型思考强度与附件归属，任何失败都不留下消息和 Run 记录。
        self.model_service.validate_reasoning_effort(
            account_id=account_id, app_id=app_id, created_by=created_by,
            invoke_from=invoke_from, effort=run_input.reasoning_effort,
            selection=run_input.model_selection,
            definition=definition,
        )
        attachments = self.attachment_service.resolve(
            run_input.file_ids,
            owner_id=account_id,
        )
        # 2.创建不可变的本轮输入，内存 Invocation 组合身份、内容和运行选项。
        message = self.conversation_service.create_agent_turn(
            account_id=account_id, app_id=app_id,
            conversation_id=conversation_id, created_by=created_by,
            invoke_from=invoke_from, query=run_input.query, image_urls=run_input.image_urls,
            attachments=dump_agent_attachments(attachments),
        )
        invocation = AgentInvocation(
            account_id=account_id, app_id=app_id, conversation_id=conversation_id,
            created_by=created_by, end_user_id=end_user_id, invoke_from=invoke_from,
            turn_id=message.id,
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
        # 3.只有需要中途问人的任务才记录配置指纹，用于恢复时判断应用配置是否已变更。
        if definition is not None:
            invocation = invocation.model_copy(
                update={"definition_fingerprint": definition.fingerprint()}
            )
        # 4.新 Run 从该入口已持久化的最大序号之后开始编号，保证事件序号跨 Run 单调递增。
        sequence = int(
            self.db.session.query(func.max(AgentExecution.last_event_sequence))
            .filter(
                AgentExecution.conversation_id == conversation_id,
                AgentExecution.invoke_from == invoke_from.value,
            )
            .scalar()
            or 0
        )
        # 5.建立本地执行句柄并在调用方事务中写入平台 Run 行，事件序号起点同时固化在两边。
        run = AgentExecutionHandle(
            run_id=uuid4(),
            conversation_id=conversation_id,
            spec=invocation,
            initial_event_sequence=sequence,
            last_event_sequence=sequence,
        )
        row = AgentExecution(
            id=run.run_id,
            app_id=run.spec.app_id,
            conversation_id=run.conversation_id,
            invoke_from=run.spec.invoke_from.value,
            turn_id=run.spec.turn_id,
            input_message_id=run.spec.input_message_id,
            status=AgentExecutionStatus.RUNNING.value,
            last_event_sequence=run.initial_event_sequence,
            created_at=datetime.now(UTC).replace(tzinfo=None),
            execution_options=run.spec.execution_options(),
            pending_inputs=[], input_history=[], display_state={},
        )
        # 6.由调用方事务 flush，保证后续事件可以按 Run 主键写入。
        self.db.session.add(row)
        self._acquire_execution(row, run)
        self.db.session.flush()
        return run, sequence

    def _lock_conversation(
        self, *, app_id: UUID, conversation_id: UUID,
    ) -> None:
        """锁定当前会话，保证同一会话的启动检查和创建串行执行。"""
        conversation = (
            self.db.session.query(Conversation.id)
            .filter(
                Conversation.id == conversation_id,
                Conversation.app_id == app_id,
                Conversation.is_deleted.is_(False),
            )
            .with_for_update()
            .one_or_none()
        )
        if conversation is None:
            raise NotFoundException("会话不存在或已删除")

    def _finalize_run(
        self,
        run: AgentExecutionHandle,
        status: AgentExecutionStatus,
        *,
        message_updates: Mapping[str, Any] | None = None,
        usage: Mapping[str, Any] | None = None,
        events: list[AgentProtocolEvent] | None = None,
        state_snapshot: Mapping[str, Any] | None = None,
    ) -> bool:
        """在 Run 锁内一次结算片段用量、消息与事件，重复或过期执行不能再次写入。"""
        if status.value not in (*AGENT_EXECUTION_TERMINAL_STATUSES, AgentExecutionStatus.WAITING_INPUT.value):
            raise ValueError("执行片段只能结算为等待输入或终态")
        # 1.锁定 Run，执行权失效或已写入终态时忽略迟到的结果。
        with self.db.auto_commit():
            row = self._lock_execution(run, with_snapshot=state_snapshot is not None)
            if row is None:
                return False
            if usage and usage.get("trace_id") != str(row.id):
                raise ValueError("不能结算其他 Run 的用量")

            # 2.仅第一次成功收敛的调用保存运行结果和结束时间。
            now = datetime.now(UTC).replace(tzinfo=None)
            row.status = status.value
            row.finished_at = None if status is AgentExecutionStatus.WAITING_INPUT else now
            # 失败提示同时落到 Run，运行列表不必回查消息即可解释失败原因。
            row.error_message = str(message_updates.get("error") or "") if message_updates else ""
            if state_snapshot is not None:
                row.apply_snapshot(state_snapshot)
            # 与累计用量同事务撤销执行权，暂停后重试也不能重复结算。
            row.executor_id = None
            row.pending_inputs = (
                list((state_snapshot or {}).get("pending_inputs", []))
                if status is AgentExecutionStatus.WAITING_INPUT else []
            )

            # 3.业务消息和 Run 在同一事务内更新。
            updates = dict(message_updates or {})
            message = self.db.session.get(ConversationTurn, row.turn_id, populate_existing=True)
            if message is None:
                raise FailException("Agent 运行对应的消息不存在")
            for field, value in updates.items():
                if run.resume is not None and field == "latency":
                    value += getattr(message, field, 0) or 0
                setattr(message, field, value)
            message.apply_usage(usage, accumulate=True)
            # 片段回答已写入 Turn，暂停和终态不再保留同一份临时消息投影。
            row.display_state = {
                key: value for key, value in (row.display_state or {}).items()
                if key != "messages"
            }
            self._append_events(row, events or [])
        return True

    def _record_events(
        self,
        run: AgentExecutionHandle,
        events: list[AgentProtocolEvent],
    ) -> None:
        """一次事务更新 Run 快照并追加一批需要历史恢复的事件。"""
        # 1.空批次直接返回，不开启事务。
        if not events:
            return

        # 2.只取本批最新的根快照，子图快照不能覆盖主图状态。
        latest_snapshot = None
        snapshot_sequence = -1
        for event in events:
            is_root_snapshot = event.method == "values" and not event.params.namespace
            if is_root_snapshot and isinstance(event.params.data, dict):
                if event.seq > snapshot_sequence:
                    latest_snapshot = event.params.data
                    snapshot_sequence = event.seq

        # 3.一个事务内更新快照并幂等追加事件；执行权失效说明已被接管，不能再写入。
        with self.db.auto_commit():
            row = self._lock_execution(run, with_snapshot=latest_snapshot is not None)
            if row is None:
                raise FailException("Agent 运行已失效，不能继续发布事件")
            previous_sequence = int(row.last_event_sequence or 0)
            # 迟到批次的快照不能回退已提交的更新状态。
            if latest_snapshot is not None and snapshot_sequence >= previous_sequence:
                row.apply_snapshot(latest_snapshot)
            self._append_events(row, events)

    def _settle_orphan_runs(
        self,
        *,
        alive_run_ids: Collection[UUID] | None = None,
        conversation_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> None:
        """收敛本进程已不再持有的运行记录；终态、消息和公开事件在同一事务提交。

        存活集合默认取进程内注册表，调用方只传业务筛选条件。
        """
        now = datetime.now(UTC).replace(tzinfo=None)
        alive = alive_run_ids if alive_run_ids is not None else self.tracker.active_run_ids()
        with self.db.auto_commit():
            # 1.候选集是所有活动状态的行，再按存活集合与业务筛选条件收窄。
            query = self.db.session.query(AgentExecution).filter(
                AgentExecution.status.in_(AGENT_EXECUTION_ACTIVE_STATUSES),
            )
            if alive:
                query = query.filter(AgentExecution.id.notin_(alive))
            if conversation_id is not None:
                query = query.filter(AgentExecution.conversation_id == conversation_id)
            if run_id is not None:
                query = query.filter(AgentExecution.id == run_id)
            # 2.逐行加锁重读，避免与刚刚提交终态的执行线程互相覆盖。
            for row in query.populate_existing().with_for_update().all():
                # 3.已请求取消的执行按取消收敛，否则说明执行已随进程消失。
                cancelled = row.cancel_requested_at is not None
                row.status = (
                    AgentExecutionStatus.CANCELLED.value
                    if cancelled
                    else AgentExecutionStatus.FAILED.value
                )
                public_error = "Agent 执行已中断，请重新发送消息。"
                row.finished_at = now
                row.error_message = "" if cancelled else public_error
                # 撤销执行权，使迟到的事件和结果无法覆盖收敛后的状态。
                row.executor_id = None
                row.pending_inputs = []
                # 4.补一条生命周期事件并同步消息状态，客户端重连才能看到真实终态。
                sequence = int(row.last_event_sequence or 0) + 1
                event = AgentProtocolEvent(
                    type="event",
                    event_id=f"{row.id}:{sequence}",
                    seq=sequence,
                    method="lifecycle",
                    params={"data": {
                        "event": "interrupted" if cancelled else "failed",
                        "status": row.status,
                        "error": public_error,
                        "message_id": str(row.turn_id),
                        "input_message_id": row.input_message_id,
                    }},
                )
                self._append_events(row, [event])
                message = self.db.session.get(ConversationTurn, row.turn_id)
                if message is not None:
                    message.status = MessageStatus.STOP.value if cancelled else MessageStatus.ERROR.value
                    message.error = public_error

    def get_run(self, run_id: UUID) -> AgentExecution | None:
        """只读取最新数据库状态；失联恢复和 Session 生命周期由调用方负责。"""
        return self.db.session.get(AgentExecution, run_id, populate_existing=True)

    def get_run_status(self, run_id: UUID) -> str | None:
        """只读取状态列，避免重复传输和解析运行快照。"""
        return self.db.session.query(AgentExecution.status).filter(AgentExecution.id == run_id).scalar()

    def get_run_metadata(self, *, conversation_id: UUID, invoke_from: InvokeFrom) -> AgentProtocolRunMetadataResp:
        """停止操作只需要运行标识与状态，不读取消息和 JSON 快照。"""
        row = (
            self.db.session.query(AgentExecution.id, AgentExecution.status, AgentExecution.last_event_sequence)
            .filter(AgentExecution.conversation_id == conversation_id, AgentExecution.invoke_from == invoke_from.value)
            .order_by(AgentExecution.created_at.desc(), AgentExecution.id.desc())
            .first()
        )
        return AgentProtocolRunMetadataResp(
            run_id=row.id, status=row.status, last_event_sequence=row.last_event_sequence,
        ) if row else AgentProtocolRunMetadataResp()

    def _request_cancel(self, row: AgentExecution) -> bool:
        """运行中先请求停止；待填任务直接结束，旧表单不能恢复原 Run。"""
        with self.db.auto_commit():
            # 1.重新读取并锁定目标状态，避免取消请求覆盖已经结束的任务。
            current = (
                self.db.session.query(AgentExecution)
                .populate_existing()
                .with_for_update()
                .filter(
                    AgentExecution.id == row.id,
                    AgentExecution.status.in_(AGENT_EXECUTION_OCCUPYING_STATUSES),
                )
                .one_or_none()
            )
            if current is None:
                latest_status = (
                    self.db.session.query(AgentExecution.status)
                    .filter(AgentExecution.id == row.id)
                    .scalar()
                )
                if latest_status is not None:
                    row.status = latest_status
                return False

            # 2.重复点击不刷新取消时刻，执行线程只认首次请求。
            if current.cancel_requested_at is None:
                current.cancel_requested_at = datetime.now(UTC).replace(tzinfo=None)

            # 3.有执行实例时只记录请求，资源释放和终态由执行线程负责。
            if current.status != AgentExecutionStatus.WAITING_INPUT.value:
                row.cancel_requested_at = current.cancel_requested_at
                return True

            # 4.待填任务直接结束；后续新输入由 LangGraph 丢弃旧调度并保留消息历史。
            current.status = AgentExecutionStatus.CANCELLED.value
            current.finished_at = datetime.now(UTC).replace(tzinfo=None)
            current.pending_inputs = []
            current.executor_id = None
            # 5.表单清空、消息停止和结束事件必须一次提交，重连才能恢复相同状态。
            events = self._state_transition_events(current, {
                "event": "interrupted", "status": "cancelled", "reason": "cancel_requested",
                "message_id": str(current.turn_id), "input_message_id": current.input_message_id,
            })
            self._append_events(current, events)
            message = self.db.session.get(ConversationTurn, current.turn_id)
            if message is not None:
                message.status = MessageStatus.STOP.value
            row.status = AgentExecutionStatus.CANCELLED.value
            row.cancel_requested_at = current.cancel_requested_at
            return True

    def _acquire_execution(self, row: AgentExecution, run: AgentExecutionHandle) -> None:
        """调用方持有创建权或 Run 锁；只分配新执行段，不复用已经结算的执行身份。"""
        if row.executor_id is not None:
            raise ValidateErrorException("当前任务仍有未结束的执行")
        row.executor_id = run.executor_id
        # 新执行段从零开始：上一段的取消请求不能阻止本段正常开始。
        row.cancel_requested_at = None

    def _lock_execution(
        self, run: AgentExecutionHandle, *, with_snapshot: bool = False,
    ) -> AgentExecution | None:
        """只锁定并刷新 Run，统一校验执行身份和活动状态。"""
        columns = [AgentExecution.executor_id, AgentExecution.status, AgentExecution.last_event_sequence,
                   AgentExecution.turn_id, AgentExecution.finished_at]
        if with_snapshot:
            columns.extend((AgentExecution.display_state, AgentExecution.pending_inputs, AgentExecution.input_history))
        row = self.db.session.get(
            AgentExecution, run.run_id, options=[load_only(*columns)],
            populate_existing=True, with_for_update=True,
        )
        if (row is None or row.executor_id != run.executor_id
                or row.status not in AGENT_EXECUTION_ACTIVE_STATUSES):
            return None
        return row

    def _append_events(self, row: AgentExecution, events: list[AgentProtocolEvent]) -> None:
        """调用方已锁 Run；事件追加与游标更新共用事务，重试不重复插入。"""
        if not events:
            return
        payloads = {
            event.seq: {"execution_id": row.id, "sequence": event.seq, "payload": event.model_dump(mode="json")}
            for event in events
        }
        statement = pg_insert(AgentExecutionEvent).values(list(payloads.values()))
        self.db.session.execute(statement.on_conflict_do_nothing(index_elements=["execution_id", "sequence"]))
        row.last_event_sequence = max(int(row.last_event_sequence or 0), max(payloads))

    def _state_transition_events(self, row: AgentExecution, lifecycle: Mapping[str, Any]) -> list[AgentProtocolEvent]:
        """状态转换只公开展示投影，私有调用参数不会进入事件载荷。"""
        projector = AgentProtocolEventProjector(row.id, start_sequence=int(row.last_event_sequence or 0))
        return projector.create_state_transition(select_public_snapshot(row.public_snapshot), dict(lifecycle))

    # 实时交付不等落库；独立写线程按上限或 250ms 合批，收尾时排空。
    _persistence_batch_size: ClassVar[int] = 128

    def _submit_execution(self, run: AgentExecutionHandle) -> None:
        """事务提交后投递到有界执行池，避免每个 Run 无限创建线程。

        调用方必须在提交前把 Run 登记进本地跟踪，否则并发请求会把它判成孤儿记录。
        """
        acquired = self._run_slots.acquire(blocking=False)
        try:
            # 1.没有空闲槽位说明并发已满，直接拒绝而不是无限排队占用请求线程。
            if not acquired:
                raise FailException("当前 Agent 执行繁忙，请稍后重试")
            self._run_executor.submit(
                self._execute_background_run,
                current_app._get_current_object(),
                run,
            )
        except Exception as exc:
            # 2.只归还本次真正占用的执行槽；本地登记由调用方反登记，失败终态由兜底结算落库。
            if acquired:
                self._run_slots.release()
            self._commit_failure(run, exc)
            raise

    def _execute_run(self, run: AgentExecutionHandle) -> None:
        """编排执行、尾批交付和终态保存，本地登记始终由最外层清理。"""
        started_at = time.monotonic()
        observer = PlatformAgentRunObserver(run.run_id)
        projector = AgentProtocolEventProjector(
            run.run_id,
            start_sequence=run.initial_event_sequence,
        )
        events = AgentExecutionEventBuffer(
            run, self._record_events,
            started_at=started_at, batch_size=self._persistence_batch_size,
            publish_realtime=lambda event: self.tracker.publish_events(run, [event]),
            worker_context=current_app._get_current_object().app_context,
        )
        result: AgentExecutionResult | None = None
        try:
            # 1.启动或执行失败也转成结果，后续仍需刷新尾批、保存终态。
            try:
                result = self._execute_agent(run, observer, projector, events)
            except Exception as exc:
                observer.finish()
                result = AgentExecutionResult(
                    status="failed",
                    error=exc,
                    usage=observer.build_run_usage(),
                )

            # 2.先提交 Core 已交付事件的尾批，再保存业务终态；刷新失败时保留原始执行错误。
            try:
                events.drain_and_close()
            except Exception as exc:
                if result.error is not None:
                    logger.exception("刷新 Agent 事件批次失败，保留原始执行错误")
                else:
                    result = replace(
                        result,
                        status="failed",
                        error=exc,
                    )

            # 3.一次提交消息、运行状态和结束事件，成功后再通知订阅者。
            self._commit_result(
                run,
                projector,
                result=result,
                latency=max(0.0, time.monotonic() - started_at),
            )
        except Exception as exc:
            # 4._finish_run 之前任一步失败都走唯一兜底；已提交过终态时 finalize 因行锁校验返回 False，不会覆盖。
            try:
                observer.finish()
            except Exception:
                logger.exception("关闭用量观测失败 run_id=%s", run.run_id)
            self._commit_failure(
                run,
                result.error if result is not None and result.error is not None else exc,
                projector=projector,
                usage=observer.build_run_usage(),
                latency=max(0.0, time.monotonic() - started_at),
            )
        finally:
            # 5.先回收写线程，再释放运行登记；旧线程不能在下一次恢复后继续提交。
            events.close()
            self.tracker.finish(run)

    def _execute_agent(
        self,
        run: AgentExecutionHandle,
        observer: PlatformAgentRunObserver,
        projector: AgentProtocolEventProjector,
        events: AgentExecutionEventBuffer,
    ) -> AgentExecutionResult:
        """集中完成启动、装配和执行；提前取消只结束本阶段，不跳过外层结果保存。"""
        # 1.启动事务统一处理取消与执行权校验；取消结果仍由外层正常保存。
        if not self._mark_run_running(run):
            observer.finish()
            return AgentExecutionResult(
                status="cancelled",
                usage=observer.build_run_usage(),
            )

        # 2.先公开运行状态；恢复执行时清空待填表单，保留已提交回答。
        events.emit(projector.create_business_event("lifecycle", {
            "event": "running",
            "phase": "preparing",
            "message_id": str(run.spec.turn_id),
            "input_message_id": run.spec.input_message_id,
        }))
        if run.resume is not None:
            row = self.get_run(run.run_id)
            snapshot = {}
            if row is not None:
                snapshot = row.public_snapshot
            events.emit(projector.create_business_event("values", {
                "pending_inputs": [],
                "input_history": snapshot.get("input_history", []),
            }))

        # 3.装配完成后释放查询 Session，避免模型执行期间占用数据库连接。
        try:
            agent, input_message = self.factory.create_executor(
                run.spec, observer=observer,
            )
        finally:
            self.db.session.close()

        # 4.监控线程和图执行具有相同的存活范围。
        return self._execute_with_cancel_monitor(run, agent, input_message, projector, events)

    def _mark_run_running(self, run: AgentExecutionHandle) -> bool:
        """事务前后都检查取消；数据库取消记录优先于本进程尚未送达的通知。"""
        if run.cancel_requested.is_set():
            return False
        with self.db.auto_commit():
            current = self._lock_execution(run)
            if current is None:
                raise FailException("Agent 运行已失效，请重新发送消息")
            if current.cancel_requested_at is not None:
                run.cancel_requested.set()
                return False
            current.status = AgentExecutionStatus.RUNNING.value
        # 事务等待和提交期间也可能收到取消，装配前必须重新读取信号。
        return not run.cancel_requested.is_set()

    def _execute_with_cancel_monitor(
        self,
        run: AgentExecutionHandle,
        agent: AgentExecutor,
        input_message: Any,
        projector: AgentProtocolEventProjector,
        events: AgentExecutionEventBuffer,
    ) -> AgentExecutionResult:
        """图执行期间保持取消监控；启动失败释放 Agent，执行退出后停止监控。"""
        done = threading.Event()
        monitor = threading.Thread(
            target=self._watch_cancel,
            args=(run, agent, done),
            daemon=True,
            name=f"agent-cancel-{run.run_id}",
        )
        try:
            events.emit(projector.create_business_event("lifecycle", {
                "event": "running", "phase": "executing",
                "message_id": str(run.spec.turn_id),
                "input_message_id": run.spec.input_message_id,
            }))
            monitor.start()
        except Exception:
            try:
                agent.close()
            except Exception:
                logger.exception("取消监控启动失败后释放 Agent 资源失败 run_id=%s", run.run_id)
            raise

        def deliver_event(agent_event: AgentEvent) -> None:
            """串联事件投影与交付；缓冲层只接收最终协议事件。"""
            event = projector.project_agent_event(agent_event, turn_id=run.spec.turn_id)
            if event is not None:
                is_text_delta = agent_event.message is not None and agent_event.message.has_text_delta
                events.emit(event, is_text_delta=is_text_delta)

        # Core 根据消息或恢复答案构造框架输入，并在执行结束后释放资源。
        try:
            return agent.execute(
                message=input_message if run.resume is None else None,
                resume=run.resume,
                on_event=deliver_event,
                cancel_requested=run.cancel_requested,
            )
        finally:
            # 监控只等待本地信号；限时回收，避免拖住最终结果落库。
            done.set()
            monitor.join(timeout=1)
            if monitor.is_alive():
                logger.warning("取消监控尚未退出，待当前操作结束后清理 run_id=%s", run.run_id)

    def _commit_result(
        self,
        run: AgentExecutionHandle,
        projector: AgentProtocolEventProjector,
        *,
        result: AgentExecutionResult,
        latency: float,
    ) -> None:
        """在同一事务中保存消息、快照和终态事件，提交后通知失败不改写结果。"""
        # 1.按执行结果确定消息状态和公开提示；未知异常详情只用于内部排障。
        status = AgentExecutionStatus(result.status)
        message_updates, lifecycle = self._prepare_result_status(run, status, result.error)

        # 2.只保留界面所需的图状态，验证待填表单并沿用已提交的答案历史。
        row = self.get_run(run.run_id)
        previous_snapshot = (row.public_snapshot if row is not None else None) or {}
        display_state = {
            key: result.state[key] for key in ("todos", "files", "async_tasks")
            if key in result.state
        }
        state = adapt_graph_state(display_state)
        pending_inputs = []
        for item in result.interrupts:
            pending_input = PendingUserInput.model_validate(item)
            pending_inputs.append(pending_input.model_dump(mode="json"))
        state.update(
            pending_inputs=pending_inputs,
            input_history=list(previous_snapshot.get("input_history", [])),
        )

        # 3.按状态、表单请求、生命周期排序，避免客户端先收到尚未保存的表单。
        events = projector.create_state_transition(state, lifecycle)

        # 4.回答、用量、Run 状态和事件一次提交；旧执行实例无权覆盖已收敛状态。
        if result.answer:
            message_updates["answer"] = result.answer
        message_updates["latency"] = latency
        committed = self._finalize_run(
            run,
            status,
            message_updates=message_updates,
            usage=result.usage,
            events=events,
            state_snapshot=state,
        )
        if not committed:
            return

        # 5.提交之后才推进本地游标、通知订阅者；通知失败保留已提交的业务结果。
        try:
            if events:
                run.last_event_sequence = events[-1].seq
                self.tracker.publish_events(run, events)
        except Exception:
            logger.exception(
                "Agent 工作台终态事件发布失败 run_id=%s，保留已持久化终态",
                run.run_id,
            )

    def _prepare_result_status(
        self,
        run: AgentExecutionHandle,
        status: AgentExecutionStatus,
        error: Exception | None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """确定业务消息状态和公开终态，内部异常只记录到日志。"""
        message_updates: dict[str, Any]
        error_code = None
        # 1.正常结束和等待输入都用普通消息状态，只有取消和失败才改消息终态。
        if status in (AgentExecutionStatus.WAITING_INPUT, AgentExecutionStatus.COMPLETED):
            message_updates = {"status": MessageStatus.NORMAL}
        elif status is AgentExecutionStatus.CANCELLED:
            message_updates = {"status": MessageStatus.STOP}
        else:
            # 2.失败时把内部异常翻译成可对外展示的文案和错误码，不泄露堆栈。
            public_error = "Agent 运行失败"
            error_code = HttpCode.FAIL.value
            if isinstance(error, CustomException):
                public_error = error.message or public_error
                error_code = error.code.value
            elif isinstance(error, AgentMessageError):
                public_error = f"模型工具调用格式错误：{error}"
            message_updates = {"status": MessageStatus.ERROR, "error": public_error}
            if isinstance(error, AGENT_PERSISTENCE_ERRORS):
                logger.error(
                    "Agent PostgreSQL 持久化失败 error_type=%s checkpoint_pool_stats=%s",
                    type(error).__name__,
                    get_postgres_pool_stats(),
                )
            if error is not None:
                logger.error(
                    "Agent 工作台运行失败",
                    exc_info=(type(error), error, error.__traceback__),
                )

        # 3.直接生成本轮公开生命周期，status 保留业务状态，event 兼容前端协议。
        lifecycle: dict[str, Any] = {
            "event": status.value,
            "status": status.value,
            "message_id": str(run.spec.turn_id),
            "input_message_id": run.spec.input_message_id,
        }
        if status is AgentExecutionStatus.WAITING_INPUT:
            lifecycle.update(event="interrupted", reason="user_input_required")
        elif status is AgentExecutionStatus.CANCELLED:
            lifecycle.update(event="interrupted", reason="cancel_requested")
        elif status is AgentExecutionStatus.FAILED:
            lifecycle["error"] = message_updates.get("error") or "Agent 运行失败"
            if error_code is not None:
                lifecycle["code"] = error_code
        return message_updates, lifecycle

    def _execute_background_run(self, flask_app: Flask, run: AgentExecutionHandle) -> None:
        """后台线程使用独立应用上下文和 Session，退出时兜底收敛失败状态。"""
        with flask_app.app_context():
            try:
                self._execute_run(run)
            except Exception as exc:
                self._commit_failure(run, exc)
            finally:
                self.db.session.remove()
                self._run_slots.release()

    def _watch_cancel(
        self,
        run: AgentExecutionHandle,
        agent: AgentExecutor,
        done: threading.Event,
    ) -> None:
        """把本进程的取消请求转成图排空；阻塞中的工具只有主动排空才会立刻停止。"""
        while not done.is_set():
            if run.cancel_requested.is_set() and agent.request_cancel():
                return
            # 图尚未进入运行态时按固定节奏重试；取消标志已置位也要等待而不是空转。
            if done.wait(0.5):
                return

    def _commit_failure(
        self,
        run: AgentExecutionHandle,
        exc: Exception,
        *,
        projector: AgentProtocolEventProjector | None = None,
        usage: Mapping[str, Any] | None = None,
        latency: float = 0.0,
    ) -> None:
        """唯一的失败兜底结算；只落库终态，不碰本地登记，自身永不抛异常。"""
        try:
            # 1.调用方没带投影器时补一个，序号接在已持久化事件之后。
            if projector is None:
                projector = AgentProtocolEventProjector(
                    run.run_id,
                    start_sequence=int(run.last_event_sequence or 0),
                )
            # 2.复用正常结算路径，终态行锁会拒绝覆盖已提交的终态。
            self._commit_result(
                run,
                projector,
                result=AgentExecutionResult(status="failed", error=exc, usage=usage or {}),
                latency=max(0.0, latency),
            )
        except Exception:
            # 3.兜底结算自身失败只能记日志，不能再向上抛给后台线程。
            logger.exception("保存 Agent Run 失败终态失败 run_id=%s", run.run_id)
        logger.error(
            "Agent 工作台后台执行失败",
            exc_info=(type(exc), exc, exc.__traceback__),
        )


__all__ = ["AgentExecutionService"]
