"""工作台事件订阅、重放和 SSE 流编排。"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import UUID

from injector import inject
from redis.exceptions import RedisError

from internal.core.agent_v2.protocol.event_projector import (
    AgentProtocolEventProjector,
)
from internal.entity import (
    WORKBENCH_TERMINAL_STATUSES,
    WorkbenchRun,
)
from internal.entity.conversation_entity import InvokeFrom
from internal.model import AgentRun
from internal.repository import (
    AgentWorkbenchRedisRepository,
    WorkbenchReplayCursorExpired,
)
from internal.schema.agent_workbench_schema import (
    WorkbenchEvent,
    WorkbenchSubscriptionReq,
)
from pkg.sqlalchemy import SQLAlchemy

from .agent_workbench_event_queue import AgentWorkbenchEventQueue
from .agent_workbench_run_service import AgentWorkbenchRunService


def _is_root_terminal_event(event: WorkbenchEvent) -> bool:
    """只有根 namespace 的应用终态才能结束整个 Run 订阅。"""
    if event.method != "lifecycle" or event.params.namespace:
        return False
    data = event.params.data
    if not isinstance(data, Mapping):
        return False
    # Skill 生命周期复用 lifecycle channel，但其 completed/failed/cancelled
    # 只表示 Skill 文件读取状态，不能结束整个 Run 订阅。
    event_name = data.get("event")
    if isinstance(event_name, str) and event_name.startswith("skill_"):
        return False
    return any(
        data.get(field_name) in WORKBENCH_TERMINAL_STATUSES
        for field_name in ("status", "event")
    )


@inject
@dataclass
class AgentWorkbenchStreamService:
    """统一读取本地队列、Redis Stream 和数据库降级事件。"""

    db: SQLAlchemy
    run_service: AgentWorkbenchRunService
    redis_repository: AgentWorkbenchRedisRepository
    event_queue: AgentWorkbenchEventQueue

    _db_poll_interval: ClassVar[float] = 5.0
    _sse_heartbeat_interval: ClassVar[float] = 15.0

    def subscribe(
        self,
        *,
        invoke_from: InvokeFrom,
        thread_id: UUID,
        subscription: WorkbenchSubscriptionReq,
    ) -> Iterator[dict[str, Any] | None]:
        """订阅线程事件，自动选择本地、Redis 或数据库读取路径。"""
        return self._subscribe_events(
            invoke_from=invoke_from,
            thread_id=thread_id,
            subscription=subscription,
        )

    def _stream_db_run(
        self,
        db_run: AgentRun,
        *,
        since: int,
        subscription: WorkbenchSubscriptionReq,
    ):
        """从 Redis 重放并跟随其他进程执行的运行，故障时回退数据库。"""
        cursor = since
        heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
        redis_repository = self.redis_repository
        terminal_seen = False
        next_db_check_at = time.monotonic() + self._db_poll_interval
        # Redis Stream 可能暂时没有新事件，因此循环不能用“读一次就结束”。
        # 只有收到根 Run 终态或数据库确认终态时才退出。
        while not terminal_seen:
            try:
                replay_batch = redis_repository.read_events(
                    db_run.id,
                    cursor,
                    block_ms=1000,
                )
            except WorkbenchReplayCursorExpired as expired:
                yield self._cursor_reset_event(
                    db_run,
                    requested_sequence=cursor,
                    oldest_event_sequence=expired.oldest_event_sequence,
                ).model_dump(mode="json")
                cursor = max(0, expired.oldest_event_sequence - 1)
                continue
            events = (
                replay_batch.events
                if replay_batch is not None
                else self.run_service.list_events(
                    db_run.id,
                    after_sequence=cursor,
                )
            )
            for event in events:
                cursor = max(cursor, event.seq)
                if _is_root_terminal_event(event):
                    terminal_seen = True
                if subscription.matches(event):
                    yield event.model_dump(mode="json")

            latest = None
            now = time.monotonic()
            if replay_batch is None or (not events and now >= next_db_check_at):
                latest = self.run_service.get_run(db_run.id)
                self.db.session.close()
                next_db_check_at = now + self._db_poll_interval
                if (
                    not events
                    and latest is not None
                    and int(latest.last_event_sequence or 0) > cursor
                ):
                    events = self.run_service.list_events(
                        db_run.id,
                        after_sequence=cursor,
                    )
                    for event in events:
                        cursor = max(cursor, event.seq)
                        if _is_root_terminal_event(event):
                            terminal_seen = True
                        if subscription.matches(event):
                            yield event.model_dump(mode="json")
            status = latest.status if latest is not None else db_run.status
            if terminal_seen or (status in WORKBENCH_TERMINAL_STATUSES and not events):
                return

            if events:
                heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
            elif time.monotonic() >= heartbeat_at:
                heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
                yield None
            if replay_batch is None:
                time.sleep(self._db_poll_interval)

    def _cursor_reset_event(
        self,
        db_run: AgentRun | WorkbenchRun,
        *,
        requested_sequence: int,
        oldest_event_sequence: int,
    ) -> WorkbenchEvent:
        """构造客户端可识别的快照重置事件。"""
        run_id = getattr(db_run, "id", getattr(db_run, "run_id", "unknown"))
        event = {
            "type": "event",
            "event_id": f"{run_id}:cursor-reset:{oldest_event_sequence}",
            "seq": max(0, oldest_event_sequence - 1),
            "method": "values",
            "params": {
                "namespace": [],
                "timestamp": None,
                "data": db_run.state_snapshot or {"messages": [], "files": {}},
                "metadata": {
                    "my_dify": {
                        "cursor_reset": True,
                        "requested_sequence": requested_sequence,
                        "oldest_event_sequence": oldest_event_sequence,
                    }
                },
            },
        }
        visible = AgentProtocolEventProjector.to_public_event(event)
        if visible is None:
            raise ValueError("游标重置事件不属于工作台公开通道")
        return visible

    def _stream_local_run(
        self,
        run: WorkbenchRun,
        *,
        since: int,
        subscription: WorkbenchSubscriptionReq,
    ):
        """订阅本 worker 的有界事件缓冲。"""
        cursor = since
        heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
        # 本地 Run 的事件队列会持续追加事件；生成器直到 Run 完成且队列消费完才结束。
        while True:
            reset_event = None
            heartbeat = False
            remaining = heartbeat_at - time.monotonic()
            batch = self.event_queue.read(
                run,
                cursor=cursor,
                timeout=min(1, max(0.0, remaining)),
            )
            if batch.timed_out and time.monotonic() >= heartbeat_at:
                heartbeat = True
                heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
            oldest_event_sequence = batch.oldest_event_sequence
            if (
                oldest_event_sequence is not None
                and cursor > 0
                and cursor < oldest_event_sequence - 1
            ):
                reset_event = self._cursor_reset_event(
                    run,
                    requested_sequence=cursor,
                    oldest_event_sequence=oldest_event_sequence,
                )
                cursor = oldest_event_sequence - 1
                batch = self.event_queue.read(
                    run,
                    cursor=cursor,
                    timeout=0,
                )
            pending = list(batch.events)
            if pending:
                cursor = pending[-1].seq
            finished = batch.finished
            if reset_event is not None:
                yield reset_event.model_dump(mode="json")
            for event in pending:
                if subscription.matches(event):
                    yield event.model_dump(mode="json")
            if heartbeat and not pending:
                yield None
            if finished:
                return

    def _subscribe_events(
        self,
        *,
        invoke_from: InvokeFrom,
        thread_id: UUID,
        subscription: WorkbenchSubscriptionReq,
    ):
        """订阅工作台事件，支持跨 Run 等待和 Redis 故障降级。"""
        # SSE 生成器可能在请求上下文销毁后才被关闭；保存当前 Session 实例，
        # finally 直接关闭它，避免再次访问 Flask-SQLAlchemy 的 scopefunc。
        stream_session = self.db.session()
        key = str(thread_id)
        since = subscription.since
        consumed_run_id: str | None = None
        heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
        redis_repository = self.redis_repository
        pubsub = redis_repository.subscribe_thread(thread_id)
        next_db_check_at = 0.0
        try:
            # 一个线程可以连续提交多个 Run，订阅必须跨 Run 保持连接。
            while True:
                run = self.event_queue.get(key)
                if run is not None and str(run.run_id) != consumed_run_id:
                    yield from self._stream_local_run(
                        run,
                        since=since,
                        subscription=subscription,
                    )
                    since = max(since, int(run.last_event_sequence or 0))
                    consumed_run_id = str(run.run_id)
                    heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
                    continue

                # 连接初始化时读取一次数据库，后续由 Redis 通知发现新 Run。
                now = time.monotonic()
                should_check_db = pubsub is None and now >= next_db_check_at
                if consumed_run_id is None and pubsub is not None:
                    should_check_db = True
                if should_check_db:
                    db_run = self.run_service.get_latest_run(
                        thread_id=thread_id,
                        invoke_from=invoke_from,
                    )
                    next_db_check_at = now + self._db_poll_interval
                    if db_run is not None and str(db_run.id) != consumed_run_id:
                        yield from self._stream_db_run(
                            db_run,
                            since=since,
                            subscription=subscription,
                        )
                        since = max(since, int(db_run.last_event_sequence or 0))
                        consumed_run_id = str(db_run.id)
                        continue

                remaining = heartbeat_at - time.monotonic()
                if remaining <= 0:
                    heartbeat_at = time.monotonic() + self._sse_heartbeat_interval
                    yield None

                notified_run_id = None
                if pubsub is not None:
                    # Redis PubSub 阻塞等待新 Run，不再每 250ms 查询数据库。
                    try:
                        message = pubsub.get_message(
                            ignore_subscribe_messages=True,
                            timeout=min(1.0, max(0.1, remaining)),
                        )
                        if isinstance(message, Mapping):
                            raw_run_id = message.get("data")
                            if isinstance(raw_run_id, bytes):
                                notified_run_id = raw_run_id.decode("utf-8")
                            else:
                                notified_run_id = str(raw_run_id or "")
                    except (RedisError, OSError, TypeError, ValueError):
                        # PubSub 断开后立即切换到本进程条件变量和低频数据库降级。
                        try:
                            pubsub.close()
                        except (RedisError, OSError):
                            pass
                        pubsub = None
                        next_db_check_at = 0.0
                else:
                    # Redis 故障时保留本进程唤醒，并使用低频数据库降级检查。
                    self.event_queue.wait(timeout=min(1.0, max(0.1, remaining)))

                if not notified_run_id:
                    continue

                # 通知只携带 run_id，详细事件仍从本地缓冲或 Redis Stream 读取。
                notified_run = self.event_queue.get(key)
                if (
                    notified_run is not None
                    and str(notified_run.run_id) == notified_run_id
                ):
                    continue
                db_run = self.run_service.get_latest_run(
                    thread_id=thread_id,
                    invoke_from=invoke_from,
                )
                if db_run is not None and str(db_run.id) == notified_run_id:
                    yield from self._stream_db_run(
                        db_run,
                        since=since,
                        subscription=subscription,
                    )
                    since = max(since, int(db_run.last_event_sequence or 0))
                    consumed_run_id = str(db_run.id)
        finally:
            # 刷新、断开或异常时释放 Redis PubSub 和数据库 Session。
            if pubsub is not None:
                try:
                    pubsub.close()
                except (RedisError, OSError):
                    pass
            stream_session.close()


__all__ = ["AgentWorkbenchStreamService"]
