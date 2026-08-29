"""Agent 工作台 Redis Stream、通知和取消信号适配器。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TypeAlias
from uuid import UUID

from redis import Redis
from redis.client import PubSub
from redis.exceptions import RedisError

from internal.exception import WorkbenchReplayCursorExpired
from internal.schema import WorkbenchEvent, WorkbenchReplayBatchResp


RedisStreamEntry: TypeAlias = tuple[Any, Mapping[Any, Any]]

DEFAULT_KEY_PREFIX = "my-dify:agent-workbench:events:v2"
DEFAULT_TTL_SECONDS = 600
DEFAULT_MAX_LENGTH = 2000
MIN_TTL_SECONDS = 60
MIN_MAX_LENGTH = 100
MIN_CANCEL_TTL_SECONDS = 30


class AgentWorkbenchRedisRepository:
    """为工作台提供短期事件重放、Run 通知和跨进程取消。"""

    def __init__(
        self,
        redis_client: Redis,
        *,
        key_prefix: str = DEFAULT_KEY_PREFIX,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_length: int = DEFAULT_MAX_LENGTH,
    ) -> None:
        if not key_prefix.strip(":"):
            raise ValueError("Redis key 前缀不能为空")
        self.redis_client = redis_client
        self.key_prefix = key_prefix.rstrip(":")
        self.ttl_seconds = max(MIN_TTL_SECONDS, int(ttl_seconds))
        self.max_length = max(MIN_MAX_LENGTH, int(max_length))

    def _event_stream_key(self, run_id: UUID | str) -> str:
        """生成单个 Run 的事件 Stream key。"""
        return f"{self.key_prefix}:run:{run_id}"

    def _cancel_signal_key(self, run_id: UUID | str) -> str:
        """生成单个 Run 的取消信号 key。"""
        return f"{self.key_prefix}:cancel:{run_id}"

    def _thread_channel(self, thread_id: UUID | str) -> str:
        """生成线程级 Run 启动通知频道。"""
        return f"{self.key_prefix}:thread:{thread_id}"

    def request_cancel(self, run_id: UUID | str, *, ttl_seconds: int = 300) -> bool:
        """写入跨进程取消信号；Redis 不可用时由数据库状态兜底。"""
        try:
            self.redis_client.setex(
                self._cancel_signal_key(run_id),
                max(MIN_CANCEL_TTL_SECONDS, int(ttl_seconds)),
                "1",
            )
            return True
        except (RedisError, OSError):
            return False

    def is_cancel_requested(self, run_id: UUID | str) -> bool | None:
        """读取取消信号；返回 None 表示 Redis 当前不可用。"""
        try:
            return bool(self.redis_client.exists(self._cancel_signal_key(run_id)))
        except (RedisError, OSError):
            return None

    def publish_run_started(
        self,
        *,
        thread_id: UUID | str,
        run_id: UUID | str,
    ) -> bool:
        """通知订阅者线程出现新 Run。"""
        try:
            self.redis_client.publish(self._thread_channel(thread_id), str(run_id))
            return True
        except (RedisError, OSError):
            return False

    def subscribe_thread(self, thread_id: UUID | str) -> PubSub | None:
        """订阅线程通知；失败时返回 None 触发数据库降级。"""
        pubsub: PubSub | None = None
        try:
            pubsub = self.redis_client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(self._thread_channel(thread_id))
            return pubsub
        except (RedisError, OSError):
            if pubsub is not None:
                try:
                    pubsub.close()
                except (RedisError, OSError):
                    pass
            return None

    def append_event(self, run_id: UUID | str, event: WorkbenchEvent) -> bool:
        """追加投影事件；Redis 故障时返回 False。"""
        sequence = event.seq
        if sequence < 1:
            raise ValueError("工作台事件 seq 必须大于 0")

        try:
            stream_key = self._event_stream_key(run_id)
            self.redis_client.xadd(
                stream_key,
                {
                    "event": json.dumps(
                        event.model_dump(mode="json"),
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ),
                    "seq": str(sequence),
                },
                id=f"{sequence}-0",
                maxlen=self.max_length,
                approximate=True,
            )
            self.redis_client.expire(stream_key, self.ttl_seconds)
            return True
        except (RedisError, OSError):
            return False

    def read_events(
        self,
        run_id: UUID | str,
        after_sequence: int,
        *,
        block_ms: int = 0,
    ) -> WorkbenchReplayBatchResp | None:
        """读取游标后的事件；Redis 故障或 Stream 不存在时返回 None。"""
        cursor = max(0, int(after_sequence))
        stream_key = self._event_stream_key(run_id)
        try:
            oldest_entries = self.redis_client.xrange(
                stream_key,
                min="-",
                max="+",
                count=1,
            )
            oldest_event_sequence = 0
            if oldest_entries:
                oldest_fields = oldest_entries[0][1]
                oldest_value = oldest_fields.get("seq") or oldest_fields.get(b"seq")
                try:
                    oldest_event_sequence = int(oldest_value or 0)
                except (TypeError, ValueError):
                    oldest_event_sequence = 0
                if (
                    oldest_event_sequence
                    and cursor > 0
                    and cursor < oldest_event_sequence - 1
                ):
                    raise WorkbenchReplayCursorExpired(oldest_event_sequence)
            streams = self.redis_client.xread(
                {stream_key: f"{cursor}-0"},
                count=self.max_length,
                block=max(0, int(block_ms)) or None,
            )
        except WorkbenchReplayCursorExpired:
            raise
        except (RedisError, OSError):
            return None

        entries: list[RedisStreamEntry] = streams[0][1] if streams else []
        decoded_events = [
            decoded
            for entry in entries
            if (decoded := self._decode_event(entry)) is not None
        ]
        decoded_events.sort(key=lambda item: item[0])
        return WorkbenchReplayBatchResp(
            events=[event for sequence, event in decoded_events if sequence > cursor],
            oldest_event_sequence=(
                oldest_event_sequence
                or (decoded_events[0][0] if decoded_events else None)
            ),
        )

    def _decode_event(
        self,
        entry: RedisStreamEntry,
    ) -> tuple[int, WorkbenchEvent] | None:
        """解码一条 Stream 记录；损坏记录不会中断后续事件。"""
        _entry_id, fields = entry
        raw_event = fields.get("event") or fields.get(b"event")
        if isinstance(raw_event, bytes):
            raw_event = raw_event.decode("utf-8")
        if not raw_event:
            return None
        raw_sequence = fields.get("seq") or fields.get(b"seq")
        try:
            event = WorkbenchEvent.model_validate_json(raw_event)
            return int(raw_sequence or event.seq), event
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None


__all__ = ["AgentWorkbenchRedisRepository"]
