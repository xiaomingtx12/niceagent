"""工作台事件队列管理器。"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar
from uuid import UUID

from injector import inject

from internal.core.agent_v2.protocol.event_projector import (
    AgentProtocolEventProjector,
)
from internal.entity import (
    WORKBENCH_TERMINAL_STATUSES,
    WorkbenchRun,
    WorkbenchRunStatus,
)
from internal.entity.conversation_entity import InvokeFrom
from internal.model import AgentRun
from internal.repository import (
    AgentWorkbenchRedisRepository,
    WorkbenchReplayCursorExpired,
)
from internal.schema.agent_workbench_schema import (
    WorkbenchEvent,
    WorkbenchLocalEventBatchResp,
    WorkbenchSubscriptionReq,
)

from .agent_workbench_run_service import AgentWorkbenchRunService


@inject
@dataclass
class AgentWorkbenchEventQueueManager:
    """统一管理工作台 Run 的本地队列、事件发布和持久化事件订阅。"""

    run_service: AgentWorkbenchRunService
    redis_repository: AgentWorkbenchRedisRepository

    _runs: dict[str, WorkbenchRun] = field(init=False, default_factory=dict)
    _condition: threading.Condition = field(
        init=False,
        default_factory=threading.Condition,
    )
    _db_poll_interval: ClassVar[float] = 5.0
    _sse_heartbeat_interval: ClassVar[float] = 15.0

    # -------------------- Synchronization API --------------------

    @property
    def condition(self) -> threading.Condition:
        """返回共享条件变量，供启动去重逻辑保持原子性。"""
        return self._condition

    def wait(self, timeout: float | None) -> None:
        """等待本地 Run 或事件状态变化。"""
        with self._condition:
            self._condition.wait(timeout=timeout)

    def get(self, conversation_id: UUID | str) -> WorkbenchRun | None:
        """读取会话对应的本地活动 Run。"""
        with self._condition:
            return self._runs.get(str(conversation_id))

    # -------------------- Run and event lifecycle --------------------

    def register(self, run: WorkbenchRun) -> None:
        """注册本地 Run，并唤醒等待中的订阅者。"""
        with self._condition:
            # 1.按会话登记当前 Worker 唯一的活动 Run。
            self._runs[str(run.conversation_id)] = run

            # 2.唤醒 Run 订阅者和等待前置 Run 的执行线程。
            self._condition.notify_all()

    def finish(self, run: WorkbenchRun) -> None:
        """标记本地 Run 完成并移出活动注册表。"""
        with self._condition:
            # 1.先设置完成信号，让已持有 Run 的消费者停止等待。
            run.done.set()

            # 2.只移除仍指向当前 Run 的会话记录，并唤醒所有等待者。
            key = str(run.conversation_id)
            if self._runs.get(key) is run:
                self._runs.pop(key, None)
            self._condition.notify_all()

    def publish(self, run: WorkbenchRun, event: WorkbenchEvent) -> None:
        """追加事件到 Run 的有界缓冲并唤醒订阅者。"""
        with self._condition:
            # 1.在同一把锁内追加事件，保证消费者看到稳定的事件顺序。
            run.events.append(event)

            # 2.立即唤醒订阅者，避免等待下一次轮询。
            self._condition.notify_all()

    def request_cancel(self, conversation_id: UUID | str, run_id: UUID) -> bool:
        """向本进程内匹配的 Run 设置取消信号。"""
        with self._condition:
            # 1.确认取消目标仍是当前会话的活动 Run。
            run = self._runs.get(str(conversation_id))
            matched = run is not None and run.run_id == run_id

            # 2.设置本地取消状态并唤醒正在等待的执行或订阅线程。
            if matched and run.status.value not in WORKBENCH_TERMINAL_STATUSES:
                run.cancel_requested.set()
                run.status = WorkbenchRunStatus.CANCELLING
            self._condition.notify_all()
            return matched

    def read(
        self,
        run: WorkbenchRun,
        *,
        cursor: int,
        timeout: float | None,
    ) -> WorkbenchLocalEventBatchResp:
        """等待并读取游标之后的本地事件，同时判断队列是否已结束。"""
        with self._condition:
            # 1.等待新事件或 Run 完成；本地完成通知优先于超时。
            deadline = (
                None
                if timeout is None
                else time.monotonic() + max(0.0, timeout)
            )
            ready = False
            while True:
                pending = tuple(event for event in run.events if event.seq > cursor)
                if run.done.is_set() or pending:
                    ready = True
                    break
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)

            # 2.在同一把锁下生成批次，保证 finished 与 pending 状态一致。
            oldest_event_sequence = run.events[0].seq if run.events else None
            return WorkbenchLocalEventBatchResp(
                events=pending,
                oldest_event_sequence=oldest_event_sequence,
                timed_out=not ready,
                finished=run.done.is_set() and not pending,
            )

    # -------------------- Subscription API --------------------

    def subscribe(
        self,
        *,
        invoke_from: InvokeFrom,
        conversation_id: UUID,
        subscription: WorkbenchSubscriptionReq,
    ) -> Iterator[dict[str, Any] | None]:
        """持续发现并跟随会话中的 Run。"""
        # 1.初始化会话级游标和两个独立的轮询时钟。
        cursor = subscription.since
        last_run_id: str | None = None
        heartbeat_deadline = time.monotonic() + self._sse_heartbeat_interval
        next_database_poll = 0.0

        while True:
            # 2.优先发现本地 Run，并按固定间隔查询持久化 Run。
            now = time.monotonic()
            poll_database = now >= next_database_poll
            run = self._find_next_run(
                conversation_id=conversation_id,
                invoke_from=invoke_from,
                last_run_id=last_run_id,
                poll_database=poll_database,
            )
            if poll_database:
                next_database_poll = now + self._db_poll_interval

            # 3.跟随一个 Run 直至结束，并保留返回的最新会话游标。
            if run is not None:
                cursor = yield from self._stream_run(
                    run,
                    cursor=cursor,
                    subscription=subscription,
                )
                run_id = run.run_id if isinstance(run, WorkbenchRun) else run.id
                last_run_id = str(run_id)
                heartbeat_deadline = time.monotonic() + self._sse_heartbeat_interval
                continue

            # 4.没有新 Run 时发送 SSE 心跳，并等待队列状态变化。
            remaining = heartbeat_deadline - time.monotonic()
            if remaining <= 0:
                yield None
                heartbeat_deadline = time.monotonic() + self._sse_heartbeat_interval
                remaining = self._sse_heartbeat_interval

            self.wait(timeout=min(1.0, max(0.1, remaining)))

    def _find_next_run(
        self,
        *,
        conversation_id: UUID,
        invoke_from: InvokeFrom,
        last_run_id: str | None,
        poll_database: bool,
    ) -> WorkbenchRun | AgentRun | None:
        """优先返回当前 Worker 的 Run，按需查询跨进程 Run。"""
        # 1.本地队列可以立即发现当前 Worker 刚创建的 Run。
        local_run = self.get(conversation_id)
        if local_run is not None and str(local_run.run_id) != last_run_id:
            return local_run
        if not poll_database:
            return None

        # 2.数据库低频查询用于发现其他 Worker 创建的 Run。
        persisted_run = self.run_service.get_latest_run(
            conversation_id=conversation_id,
            invoke_from=invoke_from,
        )
        if persisted_run is not None and str(persisted_run.id) != last_run_id:
            return persisted_run
        return None

    def _stream_run(
        self,
        run: WorkbenchRun | AgentRun,
        *,
        cursor: int,
        subscription: WorkbenchSubscriptionReq,
    ) -> Generator[dict[str, Any] | None, None, int]:
        """根据 Run 所在位置选择实时缓冲或持久化事件源。"""
        # 1.进程内实体直接跟随共享事件队列。
        if isinstance(run, WorkbenchRun):
            cursor = yield from self._stream_local_run(
                run,
                cursor=cursor,
                subscription=subscription,
            )
        else:
            # 2.数据库实体通过 Redis Stream 跟随，并由数据库负责降级。
            cursor = yield from self._stream_persisted_run(
                run,
                cursor=cursor,
                subscription=subscription,
            )
        return cursor

    def _stream_local_run(
        self,
        run: WorkbenchRun,
        *,
        cursor: int,
        subscription: WorkbenchSubscriptionReq,
    ) -> Generator[dict[str, Any] | None, None, int]:
        """跟随当前 Worker 中正在追加的有界事件缓冲。"""
        # 1.为本地读取建立独立心跳周期。
        heartbeat_deadline = time.monotonic() + self._sse_heartbeat_interval

        while True:
            # 2.从当前游标开始等待增量事件或 Run 结束。
            remaining = heartbeat_deadline - time.monotonic()
            batch = self.read(
                run,
                cursor=cursor,
                timeout=min(1, max(0.0, remaining)),
            )

            # 3.游标落后于有界缓冲时，先发送状态快照再从最早事件重放。
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
                cursor = reset_event.seq
                yield reset_event.model_dump(mode="json")
                batch = self.read(run, cursor=cursor, timeout=0)

            # 4.推进游标并只发送符合当前订阅条件的事件。
            cursor, _, visible_events = self._project_event_batch(
                batch.events,
                cursor=cursor,
                subscription=subscription,
            )
            yield from visible_events

            # 5.Run 结束时返回游标；空闲达到阈值时发送心跳。
            if batch.events:
                heartbeat_deadline = time.monotonic() + self._sse_heartbeat_interval
            if batch.finished:
                return cursor
            if batch.timed_out and time.monotonic() >= heartbeat_deadline:
                yield None
                heartbeat_deadline = time.monotonic() + self._sse_heartbeat_interval

    def _stream_persisted_run(
        self,
        run: AgentRun,
        *,
        cursor: int,
        subscription: WorkbenchSubscriptionReq,
    ) -> Generator[dict[str, Any] | None, None, int]:
        """跟随 Redis Stream；Redis 不可用时读取数据库事件。"""
        # 1.分别维护 SSE 心跳和数据库状态核对周期。
        heartbeat_deadline = time.monotonic() + self._sse_heartbeat_interval
        next_progress_poll = time.monotonic() + self._db_poll_interval

        while True:
            # 2.优先阻塞读取 Redis Stream，Redis 不可用时读取数据库事件。
            try:
                events, redis_available = self._read_persisted_events(
                    run_id=run.id,
                    cursor=cursor,
                )
            except WorkbenchReplayCursorExpired as expired:
                reset_event = self._cursor_reset_event(
                    run,
                    requested_sequence=cursor,
                    oldest_event_sequence=expired.oldest_event_sequence,
                )
                cursor = reset_event.seq
                yield reset_event.model_dump(mode="json")
                continue

            # 3.投影本批事件；根 Run 终态事件可以立即结束本次跟随。
            cursor, run_finished, visible_events = self._project_event_batch(
                events,
                cursor=cursor,
                subscription=subscription,
            )
            yield from visible_events
            if run_finished:
                return cursor

            # 4.空闲或 Redis 降级期间核对数据库状态并补读已持久化事件。
            received_events = bool(events)
            now = time.monotonic()
            should_poll_progress = not redis_available or (
                not events and now >= next_progress_poll
            )
            if should_poll_progress:
                progress = self.run_service.get_run_progress(run.id)
                next_progress_poll = now + self._db_poll_interval
                if (
                    progress is not None
                    and progress.last_event_sequence > cursor
                ):
                    catch_up_events = self.run_service.list_events(
                        run.id,
                        after_sequence=cursor,
                    )
                    received_events = received_events or bool(catch_up_events)
                    cursor, run_finished, visible_events = self._project_event_batch(
                        catch_up_events,
                        cursor=cursor,
                        subscription=subscription,
                    )
                    yield from visible_events
                    if run_finished:
                        return cursor
                if (
                    progress is None
                    or progress.status in WORKBENCH_TERMINAL_STATUSES
                ):
                    return cursor

            # 5.刷新活动时间、发送心跳，并限制数据库降级轮询频率。
            if received_events:
                heartbeat_deadline = time.monotonic() + self._sse_heartbeat_interval
            elif time.monotonic() >= heartbeat_deadline:
                yield None
                heartbeat_deadline = time.monotonic() + self._sse_heartbeat_interval
            if not redis_available:
                time.sleep(self._db_poll_interval)

    def _read_persisted_events(
        self,
        *,
        run_id: UUID,
        cursor: int,
    ) -> tuple[list[WorkbenchEvent], bool]:
        """返回可用事件以及它们是否来自 Redis。"""
        # 1.Redis Stream 提供实时事件和短期重放。
        replay_batch = self.redis_repository.read_events(
            run_id,
            cursor,
            block_ms=1000,
        )
        if replay_batch is not None:
            return replay_batch.events, True

        # 2.Redis 不可用时只读取数据库中可持久化的协议事件。
        return self.run_service.list_events(run_id, after_sequence=cursor), False

    # -------------------- Event projection helpers --------------------

    def _cursor_reset_event(
        self,
        run: AgentRun | WorkbenchRun,
        *,
        requested_sequence: int,
        oldest_event_sequence: int,
    ) -> WorkbenchEvent:
        """构造客户端可识别的快照重置事件。"""
        # 1.用当前状态快照构造新的 values 事件和恢复游标。
        run_id = run.id if isinstance(run, AgentRun) else run.run_id
        event = {
            "type": "event",
            "event_id": f"{run_id}:cursor-reset:{oldest_event_sequence}",
            "seq": max(0, oldest_event_sequence - 1),
            "method": "values",
            "params": {
                "namespace": [],
                "timestamp": None,
                "data": run.state_snapshot or {"messages": [], "files": {}},
                "metadata": {
                    "my_dify": {
                        "cursor_reset": True,
                        "requested_sequence": requested_sequence,
                        "oldest_event_sequence": oldest_event_sequence,
                    }
                },
            },
        }

        # 2.通过公共事件投影器校验输出，避免绕过协议字段约束。
        visible = AgentProtocolEventProjector.to_public_event(event)
        if visible is None:
            raise ValueError("游标重置事件不属于工作台公开通道")
        return visible

    @staticmethod
    def _project_event_batch(
        events: list[WorkbenchEvent] | tuple[WorkbenchEvent, ...],
        *,
        cursor: int,
        subscription: WorkbenchSubscriptionReq,
    ) -> tuple[int, bool, list[dict[str, Any]]]:
        """推进游标，并返回当前订阅可见的序列化事件。"""
        # 1.所有事件都推进游标和终态判断，不受客户端过滤条件影响。
        run_finished = False
        visible_events: list[dict[str, Any]] = []
        for event in events:
            cursor = max(cursor, event.seq)
            run_finished = run_finished or _is_root_terminal_event(event)
            if subscription.matches(event):
                visible_events.append(event.model_dump(mode="json"))

        # 2.调用方使用统一结果继续本地或持久化事件循环。
        return cursor, run_finished, visible_events


def _is_root_terminal_event(event: WorkbenchEvent) -> bool:
    """只有根 namespace 的应用终态才能结束整个 Run 订阅。"""
    # 1.只有根命名空间的 lifecycle 事件可能表示 Run 终态。
    if event.method != "lifecycle" or event.params.namespace:
        return False
    data = event.params.data
    if not isinstance(data, Mapping):
        return False

    # 2.Skill lifecycle 只表示文件读取状态，不能结束根 Run。
    event_name = data.get("event")
    if isinstance(event_name, str) and event_name.startswith("skill_"):
        return False

    # 3.兼容 lifecycle 数据中的 status 和 event 两种终态字段。
    return any(
        data.get(field_name) in WORKBENCH_TERMINAL_STATUSES
        for field_name in ("status", "event")
    )


__all__ = ["AgentWorkbenchEventQueueManager"]
