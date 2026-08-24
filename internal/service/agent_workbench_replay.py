"""工作台协议事件的 Redis Stream 短期重放层。"""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from typing import Any, Mapping
from uuid import UUID

from redis import Redis
from redis.exceptions import RedisError


class ReplayCursorExpired(Exception):
    """Redis Stream 已裁剪掉请求游标之前的事件。"""

    def __init__(self, oldest_sequence: int) -> None:
        self.oldest_sequence = oldest_sequence
        super().__init__(f"workbench replay cursor expired before sequence {oldest_sequence}")


@dataclass(frozen=True)
class ReplayBatch:
    """一次重放查询的结果及其来源状态。"""

    events: list[dict[str, Any]]
    oldest_sequence: int | None = None


class AgentWorkbenchReplayStore:
    """将已投影事件写入 Redis Stream，并按 run 序号读取。"""

    def __init__(
        self,
        redis_client: Redis,
        *,
        key_prefix: str = "my-dify:agent-workbench:events:v1",
        # 1、工作台事件只用于短期断线恢复，默认缓存 10 分钟。
        ttl_seconds: int = 600,
        max_length: int = 2000,
    ) -> None:
        self.redis = redis_client
        self.key_prefix = key_prefix.rstrip(":")
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_length = max(100, int(max_length))

    def key(self, run_id: UUID | str) -> str:
        return f"{self.key_prefix}:run:{run_id}"

    def thread_channel(
        self,
        *,
        kind: str,
        scope: str,
        thread_id: UUID | str,
    ) -> str:
        """生成线程级通知频道，避免把发布 token 直接放进 Redis key。"""
        raw = f"{kind}:{scope}:{thread_id}".encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        return f"{self.key_prefix}:thread:{digest}"

    def publish_run_started(
        self,
        *,
        kind: str,
        scope: str,
        thread_id: UUID | str,
        run_id: UUID | str,
    ) -> bool:
        """通知订阅者有新的 run，事件正文仍从 run stream 读取。"""
        try:
            self.redis.publish(
                self.thread_channel(
                    kind=kind,
                    scope=scope,
                    thread_id=thread_id,
                ),
                str(run_id),
            )
            return True
        except (RedisError, OSError, TypeError, ValueError):
            return False

    def subscribe_thread(
        self,
        *,
        kind: str,
        scope: str,
        thread_id: UUID | str,
    ):
        """创建线程级通知订阅，由调用方在 finally 中关闭。"""
        try:
            pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(
                self.thread_channel(
                    kind=kind,
                    scope=scope,
                    thread_id=thread_id,
                )
            )
            return pubsub
        except (RedisError, OSError, TypeError, ValueError):
            return None

    def append(self, run_id: UUID | str, event: Mapping[str, Any]) -> bool:
        """追加事件并刷新窗口 TTL；Redis 故障时返回 False。"""
        try:
            sequence = int(event["seq"])
            payload = json.dumps(dict(event), ensure_ascii=True, separators=(",", ":"))
            self.redis.xadd(
                self.key(run_id),
                {
                    "event": payload,
                    "seq": str(sequence),
                    "event_id": str(event["event_id"]),
                },
                id=f"{sequence}-0",
                maxlen=self.max_length,
                approximate=True,
            )
            self.redis.expire(self.key(run_id), self.ttl_seconds)
            return True
        except (RedisError, OSError, TypeError, ValueError):
            return False

    def read_since(
        self,
        run_id: UUID | str,
        since: int,
        *,
        block_ms: int = 0,
    ) -> ReplayBatch | None:
        """读取游标后的事件；None 表示 Redis 不可用或流尚未创建。"""
        try:
            oldest_entries = self.redis.xrange(
                self.key(run_id), min="-", max="+", count=1
            )
        except (RedisError, OSError):
            return None
        if not oldest_entries:
            return None

        oldest_fields = oldest_entries[0][1]
        try:
            oldest = int(self._field(oldest_fields, "seq"))
        except (TypeError, ValueError):
            oldest = 0
        if oldest and since > 0 and since < oldest - 1:
            raise ReplayCursorExpired(oldest)
        try:
            streams = self.redis.xread(
                {self.key(run_id): f"{max(0, since)}-0"},
                count=self.max_length,
                block=max(0, int(block_ms)) or None,
            )
        except (RedisError, OSError):
            return None
        entries = streams[0][1] if streams else []

        parsed: list[tuple[int, dict[str, Any]]] = []
        for _entry_id, fields in entries:
            raw = self._field(fields, "event")
            if not raw:
                continue
            try:
                payload = json.loads(raw)
                sequence = int(self._field(fields, "seq") or payload.get("seq", 0))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                parsed.append((sequence, payload))
        if not parsed:
            return ReplayBatch(events=[])

        parsed.sort(key=lambda item: item[0])
        return ReplayBatch(
            events=[payload for sequence, payload in parsed if sequence > since],
            oldest_sequence=oldest or parsed[0][0],
        )

    @staticmethod
    def _field(fields: Mapping[Any, Any], name: str) -> str:
        value = fields.get(name)
        if value is None:
            value = fields.get(name.encode())
        if value is None:
            return ""
        return value.decode() if isinstance(value, bytes) else str(value)


__all__ = ["AgentWorkbenchReplayStore", "ReplayBatch", "ReplayCursorExpired"]
