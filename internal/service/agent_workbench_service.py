"""创建者与发布应用共用的 Agent Protocol 工作台桥接层。"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, ClassVar, Mapping
from uuid import UUID, uuid4

from flask import current_app, has_app_context
from injector import inject
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import and_, func, or_
from internal.core.agent_v2.events.protocol import ProtocolEventProjector
from internal.core.agent_v2.events.visibility import project_safe_workbench_event
from internal.entity.conversation_entity import InvokeFrom, MessageStatus
from internal.exception import ForbiddenException, NotFoundException
from internal.model import AgentRun, AgentRunEvent, App, Conversation, Message
from pkg.sqlalchemy import SQLAlchemy
from .agent_attachment_service import AgentAttachmentService
from .agent_invocation_service import AgentInvocationService, AgentInvocationSpec
from .app_config_service import AppConfigService
from .base_service import BaseService
from .conversation_service import ConversationService
from .web_app_service import WebAppService
from .agent_workbench_replay import (
    AgentWorkbenchReplayStore,
    ReplayCursorExpired,
)


@dataclass
class _PendingRun:
    run_id: UUID
    thread_id: UUID
    spec: AgentInvocationSpec
    status: str = "queued"
    initial_sequence: int = 0
    last_sequence: int = 0
    values: dict[str, Any] | None = None
    error: str = ""
    events: deque[dict[str, Any]] = None  # type: ignore[assignment]
    done: threading.Event = None  # type: ignore[assignment]
    cancel_requested: threading.Event = None  # type: ignore[assignment]
    last_cancel_poll: float = 0.0
    include_opening_statement: bool = False

    def __post_init__(self) -> None:
        self.events = deque(maxlen=2000)
        self.done = threading.Event()
        self.cancel_requested = threading.Event()


@dataclass(frozen=True)
class _RunReservation:
    run_id: UUID
    input_message_id: str | None


@inject
@dataclass
class AgentWorkbenchService(BaseService):
    """薄协议适配层；Deep Agents 仍是执行状态权威。"""

    db: SQLAlchemy
    app_config_service: AppConfigService
    agent_invocation_service: AgentInvocationService
    agent_attachment_service: AgentAttachmentService
    web_app_service: WebAppService
    redis_client: Redis

    _runs: ClassVar[dict[tuple[str, str], _PendingRun]]
    _starting_runs: ClassVar[dict[tuple[str, str], _RunReservation]]
    _lock: ClassVar[threading.RLock]
    _condition: ClassVar[threading.Condition]
    # 1、Redis 可用时不再用数据库轮询发现下一轮任务；该间隔只作为 Redis
    # 不可用时的低频降级检查间隔，避免长连接持续占用数据库连接。
    _db_poll_interval: ClassVar[float] = 5.0
    _sse_heartbeat_interval: ClassVar[float] = 15.0
    _message_page_size: ClassVar[int] = 20

    def __post_init__(self) -> None:
        self._runs = {}
        self._starting_runs = {}
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        config = current_app.config if has_app_context() else {}
        self._replay_store = AgentWorkbenchReplayStore(
            self.redis_client,
            key_prefix=config.get(
                "AGENT_WORKBENCH_REPLAY_KEY_PREFIX",
                "my-dify:agent-workbench:events:v1",
            ),
            # 2、工作台事件只用于短期断线恢复，默认保留 10 分钟。
            ttl_seconds=config.get("AGENT_WORKBENCH_REPLAY_TTL_SECONDS", 600),
            max_length=config.get("AGENT_WORKBENCH_REPLAY_MAX_LENGTH", 2000),
        )

    def _get_replay_store(self) -> AgentWorkbenchReplayStore | None:
        """获取共享 Redis replay store；连接失败时由调用方走数据库降级。"""
        return getattr(self, "_replay_store", None)

    @staticmethod
    def _scope_key(kind: str, value: str) -> str:
        return f"{kind}:{value}"

    def authorize(
        self,
        *,
        kind: str,
        scope: str,
        thread_id: UUID,
        account: Any,
        release_session: bool = True,
    ) -> None:
        """每次读取或订阅都重新校验应用与任务线程归属。"""
        if kind == "app":
            app = (
                self.db.session.query(App)
                .filter(App.id == UUID(scope), App.account_id == account.id)
                .one_or_none()
            )
            conversation = self.get(Conversation, thread_id)
            if (
                app is None
                or conversation is None
                or conversation.app_id != app.id
                or conversation.invoke_from != InvokeFrom.DEBUGGER
            ):
                raise ForbiddenException("工作台线程不存在或无权访问")
            # 4、鉴权只做短查询，完成后立即释放连接，不能把连接带进 SSE 长请求。
            # 取消接口会继续更新同一个实体，因此由调用方暂时保留 Session。
            if release_session:
                self.db.session.close()
            return
        if kind == "web_app":
            try:
                self.web_app_service.get_owned_web_conversation(
                    token=scope,
                    conversation_id=thread_id,
                    account=account,
                )
            finally:
                # 5、发布态鉴权同样不保留数据库 Session。
                if release_session:
                    self.db.session.close()
            return
        raise NotFoundException("未知工作台作用域")

    def _persist_run(self, run: _PendingRun, *, kind: str, scope: str) -> None:
        """返回 command 响应前持久化运行主体。"""
        if run.spec.app_id is None:
            raise ValueError("工作台运行缺少 app_id")
        self.db.session.add(
            AgentRun(
                id=run.run_id,
                app_id=run.spec.app_id,
                conversation_id=run.spec.conversation_id,
                thread_id=run.thread_id,
                scope_kind=kind,
                scope_value=scope,
                invoke_from=run.spec.invoke_from,
                created_by=run.spec.created_by,
                message_id=run.spec.message_id,
                status=run.status,
                query=run.spec.query,
                last_sequence=run.initial_sequence,
            )
        )
        self.db.session.flush()

    def _persist_status(self, run: _PendingRun, *, status: str, error: str = "") -> None:
        row = self.db.session.query(AgentRun).filter(AgentRun.id == run.run_id).one_or_none()
        if row is None:
            return
        now = datetime.utcnow()
        updates: dict[str, Any] = {"status": status, "error": error}
        if status == "running" and row.started_at is None:
            updates["started_at"] = now
        if status in {"completed", "failed", "cancelled"}:
            updates["finished_at"] = now
        self.update(row, **updates)

    def _persist_event(self, run: _PendingRun, event: Mapping[str, Any]) -> None:
        params = event.get("params") if isinstance(event.get("params"), Mapping) else {}
        replay_store = self._get_replay_store()
        if replay_store is not None:
            replay_store.append(run.run_id, event)

        # token/message 增量只保留在短期 Redis 窗口；最终 messages 由 values 快照持久化。
        if event.get("method") == "messages":
            return
        existing = None
        existing = (
            self.db.session.query(AgentRunEvent)
            .filter(AgentRunEvent.run_id == run.run_id, AgentRunEvent.seq == event["seq"])
            .one_or_none()
        )
        if existing is None:
            existing = (
                self.db.session.query(AgentRunEvent)
                .filter(
                    AgentRunEvent.run_id == run.run_id,
                    AgentRunEvent.event_id == event["event_id"],
                )
                .one_or_none()
            )
        inserted = existing is None
        if inserted:
            self.create(
                AgentRunEvent,
                run_id=run.run_id,
                seq=event["seq"],
                event_id=event["event_id"],
                method=event["method"],
                namespace=params.get("namespace", []),
                payload=event,
            )
        row = self.db.session.query(AgentRun).filter(AgentRun.id == run.run_id).one_or_none()
        if row is not None:
            values = params.get("data") if event.get("method") == "values" else None
            updates: dict[str, Any] = {
                "last_sequence": max(int(row.last_sequence or 0), int(event["seq"]))
            }
            if isinstance(values, dict):
                run.values = values
                updates["values"] = values
            self.update(row, **updates)

    def _load_db_run(self, *, kind: str, scope: str, thread_id: UUID) -> Any | None:
        runs = self._load_db_runs(kind=kind, scope=scope, thread_id=thread_id)
        return runs[-1] if runs else None

    def _thread_sequence_cursor(
        self,
        *,
        kind: str,
        scope: str,
        thread_id: UUID,
        close_session: bool = True,
    ) -> int:
        """返回任务线程已发布的最大序号，供下一轮继续编号。"""
        persisted = int(
            self.db.session.query(func.max(AgentRun.last_sequence))
            .filter(
                AgentRun.thread_id == thread_id,
                AgentRun.scope_kind == kind,
                AgentRun.scope_value == scope,
            )
            .scalar()
            or 0
        )
        # 6、命令创建事务中不能提前关闭 Session；独立读操作完成后再释放连接。
        if close_session:
            self.db.session.close()
        key = (self._scope_key(kind, scope), str(thread_id))
        with self._lock:
            local = self._runs.get(key)
            local_sequence = int(local.last_sequence or 0) if local else 0
        return max(persisted, local_sequence)

    def _load_db_runs(
        self,
        *,
        kind: str,
        scope: str,
        thread_id: UUID,
    ) -> list[AgentRun]:
        """按创建顺序返回任务内的所有运行。"""
        rows = (
            self.db.session.query(AgentRun)
            .populate_existing()
            .filter(
                AgentRun.thread_id == thread_id,
                AgentRun.scope_kind == kind,
                AgentRun.scope_value == scope,
            )
            .order_by(AgentRun.created_at.asc())
            .all()
        )
        # 7、返回前关闭本次只读 Session；实体字段已加载，可安全用于状态投影。
        self.db.session.close()
        return rows

    def _load_db_events(self, run_id: UUID, since: int) -> list[dict[str, Any]]:
        rows = (
            self.db.session.query(AgentRunEvent)
            .filter(AgentRunEvent.run_id == run_id, AgentRunEvent.seq > since)
            .order_by(AgentRunEvent.seq.asc())
            .all()
        )
        payloads = [row.payload for row in rows]
        # 8、事件回放只需要已加载的 payload，立即归还数据库连接。
        self.db.session.close()
        return payloads

    def _stream_db_run(
        self,
        db_run: AgentRun,
        *,
        since: int,
        body: Mapping[str, Any] | None,
    ):
        """从 Redis 重放并跟随其他进程执行的运行，故障时回退数据库。"""
        cursor = since
        heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
        terminal_statuses = {"completed", "failed", "cancelled"}
        replay_store = self._get_replay_store()
        terminal_seen = False
        while True:
            try:
                replay_batch = (
                    replay_store.read_since(db_run.id, cursor, block_ms=1000)
                    if replay_store is not None
                    else None
                )
            except ReplayCursorExpired as expired:
                yield self._cursor_reset_event(
                    db_run,
                    requested_sequence=cursor,
                    oldest_sequence=expired.oldest_sequence,
                )
                cursor = max(0, expired.oldest_sequence - 1)
                continue
            events = (
                replay_batch.events
                if replay_batch is not None
                else self._load_db_events(db_run.id, cursor)
            )
            for event in events:
                sequence = int(event.get("seq", cursor))
                cursor = max(cursor, sequence)
                if (
                    event.get("method") == "lifecycle"
                    and isinstance(event.get("params"), Mapping)
                    and isinstance(event["params"].get("data"), Mapping)
                    and event["params"]["data"].get("event") in terminal_statuses
                ):
                    # 9、终态事件已经进入流后，本轮订阅无需再查询数据库确认结束。
                    terminal_seen = True
                visible = project_safe_workbench_event(event)
                if visible is not None and self._matches_subscription(visible, body or {}):
                    yield visible

            # 10、Redis 正常时由阻塞读取等待事件；只有 Redis 不可用时才低频
            # 查询数据库状态，避免每个 SSE 连接持续占用数据库连接。
            latest = None
            if replay_batch is None:
                latest = (
                    self.db.session.query(AgentRun)
                    .populate_existing()
                    .filter(AgentRun.id == db_run.id)
                    .one_or_none()
                )
                self.db.session.close()
                if (
                    not events
                    and latest is not None
                    and int(getattr(latest, "last_sequence", 0) or 0) > cursor
                ):
                    events = self._load_db_events(db_run.id, cursor)
                    for event in events:
                        sequence = int(event.get("seq", cursor))
                        cursor = max(cursor, sequence)
                        visible = project_safe_workbench_event(event)
                        if visible is not None and self._matches_subscription(visible, body or {}):
                            yield visible
            status = latest.status if latest is not None else db_run.status
            if terminal_seen or (status in terminal_statuses and not events):
                return

            if events:
                heartbeat_at = (
                    time.monotonic() + self._sse_heartbeat_interval
                )
            elif time.monotonic() >= heartbeat_at:
                heartbeat_at = (
                    time.monotonic() + self._sse_heartbeat_interval
                )
                yield None
            if replay_batch is None:
                time.sleep(self._db_poll_interval)

    def _cursor_reset_event(
        self,
        db_run: AgentRun | _PendingRun,
        *,
        requested_sequence: int,
        oldest_sequence: int,
    ) -> dict[str, Any]:
        """构造客户端可识别的快照重置事件。"""
        run_id = getattr(db_run, "id", getattr(db_run, "run_id", "unknown"))
        event = {
            "type": "event",
            "event_id": f"{run_id}:cursor-reset:{oldest_sequence}",
            "seq": max(0, oldest_sequence - 1),
            "method": "values",
            "params": {
                "namespace": [],
                "data": db_run.values or {"messages": [], "files": {}},
                "metadata": {
                    "my_dify": {
                        "cursor_reset": True,
                        "requested_sequence": requested_sequence,
                        "oldest_sequence": oldest_sequence,
                    }
                },
            },
        }
        return project_safe_workbench_event(event) or event

    @staticmethod
    def _content_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, Mapping) and part.get("type") in {"text", None}
            )
        return str(content or "")

    def _query_from_command(
        self,
        command: Mapping[str, Any],
    ) -> tuple[str, list[str], list[str], str | None]:
        params = command.get("params") if isinstance(command.get("params"), Mapping) else command
        input_data = params.get("input") if isinstance(params, Mapping) else None
        if not isinstance(input_data, Mapping):
            input_data = params if isinstance(params, Mapping) else {}
        messages = input_data.get("messages")
        if isinstance(messages, list) and messages:
            last = messages[-1]
            if isinstance(last, Mapping):
                raw_message_id = last.get("id")
                message_id = (
                    str(raw_message_id)
                    if isinstance(raw_message_id, (str, UUID))
                    and 0 < len(str(raw_message_id)) <= 255
                    else None
                )
                return (
                    self._content_to_text(last.get("content")),
                    list(input_data.get("image_urls") or []),
                    list(input_data.get("file_ids") or []),
                    message_id,
                )
        query = input_data.get("query") or command.get("query")
        if not query:
            raise ValueError("run.start 需要 input.messages 或 query")
        return (
            str(query),
            list(input_data.get("image_urls") or []),
            list(input_data.get("file_ids") or []),
            None,
        )

    def _creator_run(
        self,
        app_id: UUID,
        thread_id: UUID,
        account: Any,
        command: Mapping[str, Any],
        run_id: UUID,
    ) -> _PendingRun:
        account_id = account.id
        app = (
            self.db.session.query(App)
            .filter(App.id == app_id, App.account_id == account_id)
            .one_or_none()
        )
        if app is None:
            raise NotFoundException("应用不存在或无权访问")
        conversation = self.get(Conversation, thread_id)
        if (
            conversation is None
            or conversation.app_id != app_id
            or conversation.invoke_from != InvokeFrom.DEBUGGER
        ):
            raise ForbiddenException("工作台线程不存在或不属于当前应用")
        query, image_urls, file_ids, input_message_id = self._query_from_command(command)
        attachments = self.agent_attachment_service.resolve(
            file_ids,
            owner_id=account_id,
        )
        conversation_id = conversation.id
        is_new = conversation.is_new
        app_name = app.name
        app_description = app.description
        message = Message(
            app_id=app_id,
            conversation_id=conversation_id,
            invoke_from=InvokeFrom.DEBUGGER,
            created_by=account_id,
            query=query,
            image_urls=image_urls,
            attachments=[attachment.to_dict() for attachment in attachments],
            status=MessageStatus.NORMAL,
        )
        self.db.session.add(message)
        self.db.session.flush()
        spec = AgentInvocationSpec(
            account_id=account_id,
            app=None,
            app_id=app_id,
            app_name=app_name,
            app_description=app_description,
            app_config={},
            conversation_id=conversation_id,
            message_id=message.id,
            created_by=account_id,
            user_id=account_id,
            end_user_id=None,
            invoke_from=InvokeFrom.DEBUGGER,
            query=query,
            image_urls=image_urls,
            attachments=attachments,
            input_message_id=input_message_id,
            opening_statement="",
        )
        return _PendingRun(
            run_id,
            thread_id,
            spec,
            include_opening_statement=is_new,
        )

    def _published_run(
        self,
        token: str,
        thread_id: UUID,
        account: Any,
        command: Mapping[str, Any],
        run_id: UUID,
    ) -> _PendingRun:
        app, end_user, conversation = self.web_app_service.get_or_create_owned_web_conversation(
            token=token, conversation_id=thread_id, account=account
        )
        app_id = app.id
        app_name = app.name
        app_description = app.description
        account_id = app.account_id
        end_user_id = end_user.id
        conversation_id = conversation.id
        is_new = conversation.is_new
        query, image_urls, file_ids, input_message_id = self._query_from_command(command)
        attachments = self.agent_attachment_service.resolve(
            file_ids,
            owner_id=account.id,
        )
        message = Message(
            app_id=app_id,
            conversation_id=conversation_id,
            invoke_from=InvokeFrom.WEB_APP,
            created_by=end_user_id,
            query=query,
            image_urls=image_urls,
            attachments=[attachment.to_dict() for attachment in attachments],
            status=MessageStatus.NORMAL,
        )
        self.db.session.add(message)
        self.db.session.flush()
        spec = AgentInvocationSpec(
            account_id=account_id,
            app=None,
            app_id=app_id,
            app_name=app_name,
            app_description=app_description,
            app_config={},
            conversation_id=conversation_id,
            message_id=message.id,
            created_by=end_user_id,
            user_id=end_user_id,
            end_user_id=end_user_id,
            invoke_from=InvokeFrom.WEB_APP,
            query=query,
            image_urls=image_urls,
            attachments=attachments,
            input_message_id=input_message_id,
            opening_statement="",
        )
        return _PendingRun(
            run_id,
            thread_id,
            spec,
            include_opening_statement=is_new,
        )

    def _hydrate_run_config(self, run: _PendingRun) -> None:
        """在后台执行阶段加载并校验完整应用配置。"""
        app_id = run.spec.app_id
        if app_id is None:
            raise ValueError("工作台运行缺少 app_id")
        try:
            app = self.db.session.query(App).filter(App.id == app_id).one_or_none()
            if app is None:
                raise NotFoundException("应用不存在")
            app_name = app.name
            app_description = app.description
            config = (
                self.app_config_service.get_draft_app_config(app)
                if run.spec.invoke_from == InvokeFrom.DEBUGGER
                else self.app_config_service.get_app_config(app)
            )
            app_config = dict(config)
            run.spec = replace(
                run.spec,
                app_config=app_config,
                app_name=app_name,
                app_description=app_description,
                opening_statement=(
                    app_config.get("opening_statement", "")
                    if run.include_opening_statement
                    else ""
                ),
            )
        finally:
            # 10、配置只在启动阶段读取一次，完成后立即归还数据库连接。
            self.db.session.close()

    @staticmethod
    def _command_success(
        command: Mapping[str, Any],
        run_id: UUID,
        applied_through_sequence: int | None = None,
    ) -> dict[str, Any]:
        response: dict[str, Any] = {
            "type": "success",
            "id": command.get("id") if isinstance(command.get("id"), int) else 0,
            "result": {"run_id": str(run_id)},
        }
        if applied_through_sequence is not None:
            response["meta"] = {"applied_through_seq": applied_through_sequence}
        return response

    @staticmethod
    def _command_error(command: Mapping[str, Any], message: str) -> dict[str, Any]:
        return {
            "type": "error",
            "id": command.get("id") if isinstance(command.get("id"), int) else None,
            "error": "invalid_argument",
            "message": message,
        }

    def command(
        self,
        *,
        kind: str,
        scope: str,
        thread_id: UUID,
        account: Any,
        command: Mapping[str, Any],
    ) -> dict[str, Any]:
        method = command.get("method")
        if method not in {"run.start", "input.inject"}:
            raise ValueError("当前工作台仅支持 run.start")
        _, _, _, input_message_id = self._query_from_command(command)
        key = (self._scope_key(kind, scope), str(thread_id))
        reservation = _RunReservation(uuid4(), input_message_id)
        with self._condition:
            active = self._runs.get(key)
            if (
                active is not None
                and input_message_id is not None
                and active.spec.input_message_id == input_message_id
            ):
                return self._command_success(
                    command,
                    active.run_id,
                    active.initial_sequence,
                )
            if active is not None and not active.done.is_set():
                return self._command_error(command, "当前任务已有正在执行的 Agent")
            starting = self._starting_runs.get(key)
            if starting is not None:
                if (
                    input_message_id is not None
                    and starting.input_message_id == input_message_id
                ):
                    return self._command_success(command, starting.run_id)
                return self._command_error(command, "当前任务已有正在启动的 Agent")
            self._starting_runs[key] = reservation

        try:
            with self.db.auto_commit():
                run = (
                    self._creator_run(
                        UUID(scope), thread_id, account, command, reservation.run_id
                    )
                    if kind == "app"
                    else self._published_run(
                        scope, thread_id, account, command, reservation.run_id
                    )
                )
                applied_through_sequence = self._thread_sequence_cursor(
                    kind=kind,
                    scope=scope,
                    thread_id=thread_id,
                    # 7、这里仍处于 command 的原子事务，交给 auto_commit 统一提交。
                    close_session=False,
                )
                run.initial_sequence = applied_through_sequence
                run.last_sequence = applied_through_sequence
                self._persist_run(run, kind=kind, scope=scope)
        except Exception:
            with self._condition:
                if self._starting_runs.get(key) is reservation:
                    self._starting_runs.pop(key, None)
                self._condition.notify_all()
            raise

        with self._condition:
            if self._starting_runs.get(key) is reservation:
                self._starting_runs.pop(key, None)
            self._runs[key] = run
            self._condition.notify_all()
        # 3、通知跨进程 SSE 订阅者有新 run；Redis 故障时仍由本进程条件变量
        # 唤醒，后续再走低频数据库降级检查。
        replay_store = self._get_replay_store()
        if replay_store is not None:
            replay_store.publish_run_started(
                kind=kind,
                scope=scope,
                thread_id=thread_id,
                run_id=run.run_id,
            )
        try:
            self._start_background_run(run)
        except Exception as exc:
            run.status = "failed"
            run.error = str(exc)
            self._persist_status(run, status="failed", error=str(exc))
            with self._condition:
                run.done.set()
                self._condition.notify_all()
            raise
        return self._command_success(
            command,
            run.run_id,
            applied_through_sequence,
        )

    @staticmethod
    def _matches_subscription(event: Mapping[str, Any], body: Mapping[str, Any]) -> bool:
        channels = body.get("channels")
        if isinstance(channels, list) and channels and event.get("method") not in channels:
            return False
        params = event.get("params") if isinstance(event.get("params"), Mapping) else {}
        namespace = params.get("namespace", [])
        namespaces = body.get("namespaces")
        if isinstance(namespaces, list) and namespaces:
            if not any(
                isinstance(prefix, list)
                and namespace[: len(prefix)] == prefix
                for prefix in namespaces
            ):
                return False
        depth = body.get("depth")
        if isinstance(depth, int) and len(namespace) > depth:
            return False
        return True

    def _start_background_run(self, run: _PendingRun) -> None:
        """在 command 所在 worker 启动生产者，SSE 只负责订阅。"""
        flask_app = current_app._get_current_object()

        def execute() -> None:
            with flask_app.app_context():
                try:
                    self._produce_run(run)
                finally:
                    self.db.session.remove()

        thread = threading.Thread(
            target=execute,
            name=f"agent-workbench-{run.run_id}",
            daemon=True,
        )
        thread.start()

    def _publish_event(
        self,
        run: _PendingRun,
        event: dict[str, Any],
    ) -> None:
        """持久化事件后通知本进程订阅者。"""
        if event["method"] == "values" and isinstance(
            event["params"].get("data"),
            dict,
        ):
            run.values = event["params"]["data"]
        run.last_sequence = int(event["seq"])
        self._persist_event(run, event)
        with self._condition:
            run.events.append(event)
            self._condition.notify_all()

    def _produce_run(self, run: _PendingRun) -> None:
        """执行 Deep Agents v3 流并写入共享重放存储。"""
        started_at = time.monotonic()
        agent = None
        projector = ProtocolEventProjector(
            run.run_id,
            start_sequence=run.initial_sequence,
            root_message_id=f"{run.spec.message_id}:assistant",
        )
        run.status = "running"
        self._persist_status(run, status="running")
        try:
            started = project_safe_workbench_event(
                projector.generated(
                    "lifecycle",
                    {"event": "started", "status": "running"},
                )
            )
            if started is not None:
                self._publish_event(run, started)
            self._hydrate_run_config(run)
            agent, llm = self.agent_invocation_service.build_agent(run.spec)
            query = llm.convert_to_human_message(
                run.spec.query,
                run.spec.image_urls,
                [attachment.to_dict() for attachment in run.spec.attachments],
            )
            if run.spec.input_message_id:
                query.id = run.spec.input_message_id
            for raw_event in agent.stream_protocol({"messages": [query]}):
                if self._is_cancelled(run):
                    break
                for event in projector.project_many(raw_event):
                    visible = project_safe_workbench_event(event)
                    if visible is not None:
                        self._publish_event(run, visible)
        except Exception as exc:
            run.status = "failed"
            run.error = str(exc)
            for pending_message in projector.finish_pending_messages():
                visible = project_safe_workbench_event(pending_message)
                if visible is not None:
                    self._publish_event(run, visible)
            failure = projector.generated(
                "lifecycle",
                {
                    "event": "failed",
                    "status": "failed",
                    "error": str(exc),
                },
            )
            visible = project_safe_workbench_event(failure)
            if visible is not None:
                self._publish_event(run, visible)
            self._persist_status(run, status="failed", error=str(exc))
            current_app.logger.exception("Agent 工作台运行失败")
        else:
            if self._is_cancelled(run):
                run.status = "cancelled"
                for pending_message in projector.finish_pending_messages():
                    visible = project_safe_workbench_event(pending_message)
                    if visible is not None:
                        self._publish_event(run, visible)
                cancelled = projector.generated(
                    "lifecycle",
                    {"event": "cancelled", "status": "cancelled"},
                )
                visible = project_safe_workbench_event(cancelled)
                if visible is not None:
                    self._publish_event(run, visible)
                self._persist_status(run, status="cancelled")
                return
            for pending_message in projector.finish_pending_messages():
                visible = project_safe_workbench_event(pending_message)
                if visible is not None:
                    self._publish_event(run, visible)
            self._persist_terminal_message(
                run,
                latency=max(0.0, time.monotonic() - started_at),
                run_usage=agent.last_run_usage if agent is not None else None,
            )
            terminal_values = dict(run.values or {})
            terminal_page = self.message_page(thread_id=run.thread_id)
            terminal_values["messages"] = terminal_page["messages"]
            terminal_values["message_page"] = terminal_page["page"]
            terminal_snapshot = project_safe_workbench_event(
                projector.generated("values", terminal_values)
            )
            if terminal_snapshot is not None:
                self._publish_event(run, terminal_snapshot)
            completed = project_safe_workbench_event(
                projector.generated(
                    "lifecycle",
                    {"event": "completed", "status": "completed"},
                )
            )
            if completed is not None:
                self._publish_event(run, completed)
            run.status = "completed"
            self._persist_status(run, status="completed")
        finally:
            with self._condition:
                run.done.set()
                self._condition.notify_all()

    def _is_cancelled(self, run: _PendingRun) -> bool:
        if run.cancel_requested.is_set():
            return True
        now = time.monotonic()
        if now - run.last_cancel_poll < self._db_poll_interval:
            return False
        run.last_cancel_poll = now
        try:
            row = (
                self.db.session.query(AgentRun)
                .populate_existing()
                .filter(AgentRun.id == run.run_id)
                .one_or_none()
            )
            return row is not None and row.status == "cancelled"
        finally:
            # 8、取消检查是短查询，不能让后台 Agent 把连接占到下一次检查。
            self.db.session.close()

    def cancel(
        self,
        *,
        thread_id: UUID,
        run_id: UUID,
        account: Any,
    ) -> None:
        """取消当前 Run，并由生产者在原生事件边界收敛。"""
        row = (
            self.db.session.query(AgentRun)
            .filter(AgentRun.id == run_id, AgentRun.thread_id == thread_id)
            .one_or_none()
        )
        if row is None:
            raise NotFoundException("运行不存在")
        self.authorize(
            kind=row.scope_kind,
            scope=row.scope_value,
            thread_id=thread_id,
            account=account,
            # 9、鉴权后还要更新 row.status，不能提前分离该实体。
            release_session=False,
        )
        if row.status in {"completed", "failed", "cancelled"}:
            return
        self.update(
            row,
            status="cancelled",
            finished_at=datetime.now(UTC).replace(tzinfo=None),
        )
        key = (self._scope_key(row.scope_kind, row.scope_value), str(thread_id))
        with self._condition:
            pending = self._runs.get(key)
            if pending is not None and pending.run_id == run_id:
                pending.cancel_requested.set()
            self._condition.notify_all()

    def _stream_local_run(
        self,
        run: _PendingRun,
        *,
        since: int,
        body: Mapping[str, Any] | None,
    ):
        """订阅本 worker 的有界事件缓冲。"""
        cursor = since
        heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
        while True:
            reset_event = None
            heartbeat = False
            with self._condition:
                while (
                    not any(event["seq"] > cursor for event in run.events)
                    and not run.done.is_set()
                ):
                    remaining = heartbeat_at - time.monotonic()
                    if remaining <= 0:
                        heartbeat = True
                        heartbeat_at = (
                            time.monotonic() + self._sse_heartbeat_interval
                        )
                        break
                    self._condition.wait(timeout=min(1, remaining))
                if run.events:
                    oldest_sequence = int(run.events[0]["seq"])
                    if cursor > 0 and cursor < oldest_sequence - 1:
                        reset_event = self._cursor_reset_event(
                            run,
                            requested_sequence=cursor,
                            oldest_sequence=oldest_sequence,
                        )
                        cursor = oldest_sequence - 1
                pending = [event for event in run.events if event["seq"] > cursor]
                if pending:
                    cursor = int(pending[-1]["seq"])
                finished = run.done.is_set() and not pending
            if reset_event is not None:
                yield reset_event
            for event in pending:
                if self._matches_subscription(event, body or {}):
                    yield event
            if heartbeat and not pending:
                yield None
            if finished:
                return

    def stream(self, *, kind: str, scope: str, thread_id: UUID, body: Mapping[str, Any] | None = None):
        """订阅工作台事件；跨轮等待使用通知，不再用数据库高频轮询。"""
        key = (self._scope_key(kind, scope), str(thread_id))
        raw_since = body.get("since", 0) if isinstance(body, Mapping) else 0
        try:
            since = max(0, int(raw_since))
        except (TypeError, ValueError):
            since = 0
        consumed_run_id: str | None = None
        heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
        replay_store = self._get_replay_store()
        pubsub = (
            replay_store.subscribe_thread(
                kind=kind,
                scope=scope,
                thread_id=thread_id,
            )
            if replay_store is not None
            else None
        )
        next_db_check_at = 0.0
        try:
            while True:
                with self._condition:
                    run = self._runs.get(key)
                if run is not None and str(run.run_id) != consumed_run_id:
                    yield from self._stream_local_run(
                        run,
                        since=since,
                        body=body,
                    )
                    since = max(since, int(run.last_sequence or 0))
                    consumed_run_id = str(run.run_id)
                    heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
                    continue

                # 11、连接初始化时读取一次数据库，后续由 Redis 通知发现新 run。
                now = time.monotonic()
                should_check_db = pubsub is None and now >= next_db_check_at
                if consumed_run_id is None and pubsub is not None:
                    should_check_db = True
                if should_check_db:
                    db_run = self._load_db_run(
                        kind=kind,
                        scope=scope,
                        thread_id=thread_id,
                    )
                    next_db_check_at = now + self._db_poll_interval
                    if db_run is not None and str(db_run.id) != consumed_run_id:
                        yield from self._stream_db_run(
                            db_run,
                            since=since,
                            body=body,
                        )
                        since = max(since, int(db_run.last_sequence or 0))
                        consumed_run_id = str(db_run.id)
                        continue

                remaining = heartbeat_at - time.monotonic()
                if remaining <= 0:
                    heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
                    yield None

                notified_run_id = None
                if pubsub is not None:
                    # 12、Redis PubSub 阻塞等待新 run，不再每 250ms 查询数据库。
                    try:
                        message = pubsub.get_message(
                            ignore_subscribe_messages=True,
                            timeout=min(1.0, max(0.1, remaining)),
                        )
                        if isinstance(message, Mapping):
                            raw_run_id = message.get("data")
                            # 13、redis-py 未开启 decode_responses 时，消息正文是 bytes。
                            if isinstance(raw_run_id, bytes):
                                notified_run_id = raw_run_id.decode("utf-8")
                            else:
                                notified_run_id = str(raw_run_id or "")
                    except (RedisError, OSError, TypeError, ValueError):
                        # 14、PubSub 断开后立即切换到本进程条件变量和低频数据库降级。
                        try:
                            pubsub.close()
                        except Exception:
                            pass
                        pubsub = None
                        next_db_check_at = 0.0
                else:
                    # 15、Redis 故障时保留本进程唤醒，并使用低频数据库降级检查。
                    with self._condition:
                        self._condition.wait(timeout=min(1.0, max(0.1, remaining)))

                if not notified_run_id:
                    continue

                # 16、通知只携带 run_id，详细事件仍从本地缓冲或 Redis Stream 读取。
                with self._condition:
                    notified_run = self._runs.get(key)
                if notified_run is not None and str(notified_run.run_id) == notified_run_id:
                    continue
                db_run = self._load_db_run(
                    kind=kind,
                    scope=scope,
                    thread_id=thread_id,
                )
                if db_run is not None and str(db_run.id) == notified_run_id:
                    yield from self._stream_db_run(
                        db_run,
                        since=since,
                        body=body,
                    )
                    since = max(since, int(db_run.last_sequence or 0))
                    consumed_run_id = str(db_run.id)
        finally:
            # 15、刷新、断开或异常时释放 Redis PubSub 和数据库 Session。
            if pubsub is not None:
                try:
                    pubsub.close()
                except Exception:
                    pass
            self.db.session.remove()

    def _persist_terminal_message(
        self,
        run: _PendingRun,
        *,
        latency: float,
        run_usage: Mapping[str, Any] | None,
    ) -> None:
        """Persist the final answer without reimplementing thought storage."""
        values = run.values or {}
        messages = values.get("messages") if isinstance(values, Mapping) else None
        answer = ""
        if isinstance(messages, list):
            for item in reversed(messages):
                if not isinstance(item, Mapping):
                    continue
                if item.get("type") not in {"ai", "assistant"}:
                    continue
                answer = self._content_to_text(item.get("content"))
                if answer:
                    break
        if not answer:
            return
        message = self.get(Message, run.spec.message_id)
        if message is None:
            return
        with self.db.auto_commit():
            message.answer = answer
            message.status = MessageStatus.NORMAL
            message.latency = latency
            for field, value in ConversationService.build_run_usage_updates(
                run_usage
            ).items():
                setattr(message, field, value)

    @staticmethod
    def _message_metrics(row: Message) -> dict[str, Any]:
        return {
            "latency": float(row.latency or 0),
            "total_token_count": int(row.total_token_count or 0),
            "input_token_count": int(row.input_token_count or 0),
            "output_token_count": int(row.output_token_count or 0),
            "cached_token_count": int(row.cached_token_count or 0),
            "reasoning_token_count": int(row.reasoning_token_count or 0),
        }

    def _project_conversation_messages(
        self,
        rows: list[Message],
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        for row in rows:
            messages.append(
                {
                    "id": f"{row.id}:human",
                    "type": "human",
                    "content": row.query,
                    "additional_kwargs": {
                        "image_urls": list(row.image_urls or []),
                        "attachments": list(row.attachments or []),
                    },
                }
            )
            if row.answer:
                messages.append(
                    {
                        "id": f"{row.id}:assistant",
                        "type": "ai",
                        "content": row.answer,
                        "additional_kwargs": {
                            "metrics": self._message_metrics(row),
                        },
                    }
                )
        return messages

    def message_page(
        self,
        *,
        thread_id: UUID,
        before: UUID | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        """按业务消息游标读取一页对话，不影响 LangGraph 完整线程状态。"""
        limit = min(max(int(page_size or self._message_page_size), 1), 50)
        query = self.db.session.query(Message).filter(
            Message.conversation_id == thread_id,
            Message.is_deleted.is_(False),
        )
        if before is not None:
            cursor = (
                self.db.session.query(Message)
                .filter(
                    Message.id == before,
                    Message.conversation_id == thread_id,
                    Message.is_deleted.is_(False),
                )
                .one_or_none()
            )
            if cursor is None:
                raise NotFoundException("历史消息游标不存在")
            query = query.filter(
                or_(
                    Message.created_at < cursor.created_at,
                    and_(
                        Message.created_at == cursor.created_at,
                        Message.id < cursor.id,
                    ),
                )
            )
        descending_rows = (
            query.order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit + 1)
            .all()
        )
        has_more = len(descending_rows) > limit
        descending_rows = descending_rows[:limit]
        rows = list(reversed(descending_rows))
        result = {
            "messages": self._project_conversation_messages(rows),
            "page": {
                "has_more": has_more,
                "next_before": (
                    str(descending_rows[-1].id)
                    if has_more and descending_rows
                    else None
                ),
            },
        }
        # 11、历史消息分页只读取已加载字段，返回前立即释放连接。
        self.db.session.close()
        return result

    def state(self, *, kind: str, scope: str, thread_id: UUID) -> dict[str, Any]:
        key = (self._scope_key(kind, scope), str(thread_id))
        with self._lock:
            run = self._runs.get(key)
        db_runs = self._load_db_runs(kind=kind, scope=scope, thread_id=thread_id)
        db_run = db_runs[-1] if db_runs else None
        source_values = (
            run.values if run and run.values is not None
            else db_run.values if db_run and db_run.values is not None
            else {}
        )
        values = dict(source_values)
        message_page = self.message_page(thread_id=thread_id)
        values["messages"] = message_page["messages"]
        values["message_page"] = message_page["page"]
        visible_message_ids = {
            UUID(str(message["id"]).removesuffix(":human"))
            for message in message_page["messages"]
            if message.get("type") == "human"
            and str(message.get("id", "")).endswith(":human")
        }
        visible_runs = [
            persisted_run
            for persisted_run in db_runs
            if persisted_run.message_id in visible_message_ids
        ]
        historical_events = [
            event
            for persisted_run in visible_runs
            for event in self._load_db_events(persisted_run.id, 0)
        ]
        known_event_ids = {
            str(event.get("event_id")) for event in historical_events
        }
        if run is not None:
            historical_events.extend(
                event
                for event in run.events
                if str(event.get("event_id")) not in known_event_ids
            )
        values["workbench_events"] = historical_events
        status = run.status if run else db_run.status if db_run else "idle"
        return {
            "values": values,
            "next": ["agent"] if status in {"queued", "running"} else [],
            "tasks": [],
            "metadata": {
                "run_id": str(run.run_id if run else db_run.id) if (run or db_run) else None,
                "status": status,
                "last_sequence": run.last_sequence if run else db_run.last_sequence if db_run else 0,
            },
        }


__all__ = ["AgentWorkbenchService"]
