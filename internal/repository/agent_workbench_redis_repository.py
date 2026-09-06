"""Agent 工作台 Redis Stream、通知和取消信号适配器。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, TypeAlias
from uuid import UUID

from redis import Redis
from redis.exceptions import RedisError

from internal.core.agent_v2.protocol.event_projector import (
    AgentProtocolEventProjector,
)
from internal.schema import WorkbenchEvent, WorkbenchReplayBatchResp


RedisStreamEntry: TypeAlias = tuple[Any, Mapping[Any, Any]]

DEFAULT_KEY_PREFIX = "my-dify:agent-workbench:events:v2"
DEFAULT_TTL_SECONDS = 600
DEFAULT_MAX_LENGTH = 2000
MIN_TTL_SECONDS = 60
MIN_MAX_LENGTH = 100
MIN_CANCEL_TTL_SECONDS = 30


class WorkbenchReplayCursorExpired(Exception):
    """Redis Stream 已裁剪掉请求游标之前的事件。"""

    def __init__(self, oldest_event_sequence: int) -> None:
        self.oldest_event_sequence = oldest_event_sequence
        super().__init__(
            "workbench replay cursor expired before sequence "
            f"{oldest_event_sequence}"
        )


class AgentWorkbenchRedisRepository:
    """为工作台提供短期事件重放和跨进程取消信号。"""

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

    def append_event(self, run_id: UUID | str, event: WorkbenchEvent) -> bool:
        """用一次 Pipeline 往返追加事件并刷新 TTL；Redis 故障时返回 False。"""
        return self.append_events(run_id, [event])

    def append_events(
        self,
        run_id: UUID | str,
        events: Sequence[WorkbenchEvent],
    ) -> bool:
        """批量追加有序事件并只刷新一次 TTL。"""
        if not events:
            return True
        if any(event.seq < 1 for event in events):
            raise ValueError("工作台事件 seq 必须大于 0")
        sequences = [event.seq for event in events]
        if sequences != sorted(set(sequences)):
            raise ValueError("工作台批量事件 seq 必须严格递增")

        try:
            stream_key = self._event_stream_key(run_id)
            # Redis 位于远端时，逐条写入会让每个流式片段承担一次网络往返；
            # 非事务 Pipeline 保留事件顺序，并把一批 XADD 和 TTL 刷新合并提交。
            with self.redis_client.pipeline(transaction=False) as pipeline:
                for event in events:
                    sequence = event.seq
                    pipeline.xadd(
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
                pipeline.expire(stream_key, self.ttl_seconds)
                results = pipeline.execute()
            return len(results) == len(events) + 1 and all(results[:-1])
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
            projected
            for entry in entries
            if (decoded := self._decode_event(entry)) is not None
            and (projected := self._project_event(decoded)) is not None
        ]
        decoded_events.sort(key=lambda item: item[0])
        oldest_event_sequence = decoded_events[0][0] if decoded_events else None
        if (
            oldest_event_sequence is not None
            and cursor > 0
            and cursor < oldest_event_sequence - 1
        ):
            raise WorkbenchReplayCursorExpired(oldest_event_sequence)
        return WorkbenchReplayBatchResp(
            events=[event for sequence, event in decoded_events if sequence > cursor],
            oldest_event_sequence=oldest_event_sequence,
        )

    @staticmethod
    def _project_event(
        decoded: tuple[int, WorkbenchEvent],
    ) -> tuple[int, WorkbenchEvent] | None:
        """对历史 Redis 事件重新执行统一公开通道投影。"""
        sequence, event = decoded
        visible = AgentProtocolEventProjector.to_public_event(event)
        return (sequence, visible) if visible is not None else None

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


__all__ = ["AgentWorkbenchRedisRepository", "WorkbenchReplayCursorExpired"]
