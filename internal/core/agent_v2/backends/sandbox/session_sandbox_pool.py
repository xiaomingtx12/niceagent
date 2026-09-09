"""进程内会话 Sandbox 租约；业务 Workspace 和数据库 Session 不进入池。"""

from __future__ import annotations

import atexit
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock, Timer
from time import monotonic
from typing import Any

from internal.core.agent_v2.backends.sandbox.sandbox_workspace_sync import SandboxProviderError
from internal.extension.logging_extension import logger


@dataclass(eq=False)
class SandboxLease:
    """一次独占租约对应的远程资源；代际标识阻止旧定时器回收新租约。"""

    key: tuple[str, str]
    dispose: Callable[[Any], None]
    sandbox: Any = None
    busy: bool = True
    invalidated: bool = False
    generation: int = 0
    timer: Any = None
    expires_at: float = 0.0


class SessionSandboxPool:
    """会话内复用空闲 Sandbox，同一会话并发占用立即失败而非共享文件系统。"""

    def __init__(
        self, *, timer_factory: Callable = Timer, max_idle: int = 3,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        """限制保留的空闲资源数量；时钟工厂使 TTL 测试无需实际等待。"""
        self._entries: dict[tuple[str, str], SandboxLease] = {}
        self._lock = RLock()
        self._timer_factory = timer_factory
        self._max_idle = max_idle
        self._closed = False
        self._clock = clock

    def acquire(
        self, key: tuple[str, str], *, create: Callable[[], Any], dispose: Callable[[Any], None],
    ) -> tuple[SandboxLease, bool]:
        """先保留会话槽位再调用网络，其他会话的创建不受慢 Provider 阻塞。"""
        expired = None
        with self._lock:
            if self._closed:
                raise SandboxProviderError("Sandbox pool is closed")
            entry = self._entries.get(key)
            if entry is not None:
                if entry.busy or entry.invalidated:
                    raise SandboxProviderError("This conversation Sandbox is already in use")
                if entry.timer is not None:
                    entry.timer.cancel()
                    entry.timer = None
                if self._clock() < entry.expires_at:
                    entry.busy = True
                    entry.generation += 1
                    return entry, True
                expired = entry
            entry = SandboxLease(key=key, dispose=dispose)
            self._entries[key] = entry
        if expired is not None:
            self._dispose(expired)
        try:
            entry.sandbox = create()
        except BaseException:
            with self._lock:
                if self._entries.get(key) is entry:
                    del self._entries[key]
            raise
        with self._lock:
            invalidated = entry.invalidated or self._closed
        if invalidated:
            self.release(entry, reusable=False, ttl=0)
            raise SandboxProviderError("Sandbox creation was cancelled")
        return entry, False

    def replace(
        self, entry: SandboxLease, *, create: Callable[[], Any], dispose: Callable[[Any], None],
    ) -> SandboxLease:
        """保留独占槽位更换失效实例，避免恢复期间另一 Run 抢占同一会话。"""
        with self._lock:
            if (self._closed or self._entries.get(entry.key) is not entry
                    or not entry.busy or entry.invalidated):
                raise SandboxProviderError("Sandbox lease is no longer available")
            replacement = SandboxLease(key=entry.key, dispose=dispose)
            self._entries[entry.key] = replacement
        self._dispose(entry)
        try:
            replacement.sandbox = create()
        except BaseException:
            with self._lock:
                if self._entries.get(entry.key) is replacement:
                    del self._entries[entry.key]
            raise
        with self._lock:
            cancelled = self._closed or replacement.invalidated
        if cancelled:
            self.release(replacement, reusable=False, ttl=0)
            raise SandboxProviderError("Sandbox replacement was cancelled")
        return replacement

    def release(self, entry: SandboxLease, *, reusable: bool, ttl: int) -> None:
        """只有同步成功的资源可以回池；淘汰和远程删除在锁外执行。"""
        discarded = []
        with self._lock:
            if self._entries.get(entry.key) is not entry or not entry.busy:
                return
            entry.busy = False
            if not reusable or entry.invalidated or self._closed or ttl <= 0:
                del self._entries[entry.key]
                discarded.append(entry)
            else:
                entry.generation += 1
                entry.expires_at = self._clock() + ttl
                # 插入顺序表示最近归还顺序，容量淘汰优先回收最久未使用者。
                del self._entries[entry.key]
                self._entries[entry.key] = entry
                try:
                    timer = self._timer_factory(ttl, self._expire, args=(entry, entry.generation))
                    timer.daemon = True
                    entry.timer = timer
                    timer.start()
                except Exception:
                    del self._entries[entry.key]
                    discarded.append(entry)
                    logger.exception("Sandbox idle timer failed")
                idle = [item for item in self._entries.values() if not item.busy]
                for old in idle[:max(0, len(idle) - self._max_idle)]:
                    del self._entries[old.key]
                    old.timer.cancel()
                    discarded.append(old)
        for old in discarded:
            self._dispose(old)

    def invalidate(self, name: str) -> None:
        """会话删除时淘汰空闲资源，正在使用的资源在本轮结束后回收。"""
        discarded = []
        with self._lock:
            for key, entry in list(self._entries.items()):
                if key[1] != name:
                    continue
                entry.invalidated = True
                if not entry.busy:
                    del self._entries[key]
                    entry.timer.cancel()
                    discarded.append(entry)
        for entry in discarded:
            self._dispose(entry)

    def close(self) -> None:
        """正常进程退出时清理已创建实例；创建中的实例返回后自行回收。"""
        with self._lock:
            self._closed = True
            entries = list(self._entries.values())
            for entry in entries:
                entry.invalidated = True
                if entry.timer is not None:
                    entry.timer.cancel()
            self._entries = {entry.key: entry for entry in entries if entry.sandbox is None}
        for entry in entries:
            if entry.sandbox is not None:
                self._dispose(entry)

    def _expire(self, entry: SandboxLease, generation: int) -> None:
        """确认资源仍空闲且租约代际相同，避免 cancel 与定时器回调竞态。"""
        with self._lock:
            if (self._entries.get(entry.key) is not entry
                    or entry.busy or entry.generation != generation):
                return
            del self._entries[entry.key]
        self._dispose(entry)

    @staticmethod
    def _dispose(entry: SandboxLease) -> None:
        """删除失败记录日志，Provider 的自动停止和删除作为进程失联兜底。"""
        try:
            entry.dispose(entry.sandbox)
        except Exception:
            logger.exception("Failed to dispose pooled Sandbox name=%s", entry.key[1])


DAYTONA_SESSION_POOL = SessionSandboxPool()
atexit.register(DAYTONA_SESSION_POOL.close)
