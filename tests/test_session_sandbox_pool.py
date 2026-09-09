"""会话 Sandbox 池的租约、TTL 和并发隔离测试。"""

import pytest

from internal.core.agent_v2.backends.sandbox.session_sandbox_pool import SessionSandboxPool
from internal.core.agent_v2.backends.sandbox.sandbox_workspace_sync import SandboxProviderError


class _Timer:
    """可控定时器，测试不需要真实等待 TTL。"""

    instances = []

    def __init__(self, delay, callback, args=()):
        self.callback, self.args, self.cancelled = callback, args, False
        self.instances.append(self)

    def start(self):
        """保持挂起，调用方手动触发回调。"""

    def cancel(self):
        """记录撤销状态。"""
        self.cancelled = True

    def fire(self):
        """执行待回收回调。"""
        if not self.cancelled:
            self.callback(*self.args)


def test_pool_reuses_only_after_release_and_expires_idle_entry():
    """同一会话释放后复用，TTL 到期后下一次重新创建。"""
    _Timer.instances = []
    pool = SessionSandboxPool(timer_factory=_Timer)
    created, disposed = [], []

    def create():
        value = object()
        created.append(value)
        return value

    lease, reused = pool.acquire(("credential", "conversation"), create=create, dispose=disposed.append)
    assert not reused
    pool.release(lease, reusable=True, ttl=60)
    lease2, reused = pool.acquire(("credential", "conversation"), create=create, dispose=disposed.append)
    assert reused and lease2.sandbox is lease.sandbox
    pool.release(lease2, reusable=True, ttl=60)
    _Timer.instances[-1].fire()
    assert disposed == [created[0]]
    lease3, reused = pool.acquire(("credential", "conversation"), create=create, dispose=disposed.append)
    assert not reused and lease3.sandbox is not lease.sandbox
    pool.close()


def test_pool_rejects_concurrent_same_conversation_and_keeps_other_sessions_independent():
    """一个会话只能有一个运行，其他会话仍可同时创建。"""
    pool = SessionSandboxPool(timer_factory=_Timer)
    lease, _ = pool.acquire(("credential", "one"), create=object, dispose=lambda _: None)
    with pytest.raises(SandboxProviderError, match="already in use"):
        pool.acquire(("credential", "one"), create=object, dispose=lambda _: None)
    other, reused = pool.acquire(("credential", "two"), create=object, dispose=lambda _: None)
    assert not reused and other is not lease
    pool.release(lease, reusable=False, ttl=0)
    pool.release(other, reusable=False, ttl=0)
    pool.close()


def test_pool_invalidate_reclaims_idle_conversation_but_waits_for_busy_run():
    """删除会话立即清理空闲实例，运行中的实例在归还时强制淘汰。"""
    disposed = []
    pool = SessionSandboxPool(timer_factory=_Timer)
    idle, _ = pool.acquire(("credential", "deleted"), create=object, dispose=disposed.append)
    pool.release(idle, reusable=True, ttl=60)
    pool.invalidate("deleted")
    assert len(disposed) == 1
    busy, _ = pool.acquire(("credential", "busy"), create=object, dispose=disposed.append)
    pool.invalidate("busy")
    pool.release(busy, reusable=True, ttl=60)
    assert len(disposed) == 2
    pool.close()


def test_delayed_timer_cannot_extend_idle_ttl():
    """定时线程未及时运行时，获取租约仍必须检查实际过期时间。"""
    now = [10.0]
    disposed = []
    pool = SessionSandboxPool(timer_factory=_Timer, clock=lambda: now[0])
    first, _ = pool.acquire(("credential", "one"), create=object, dispose=disposed.append)
    pool.release(first, reusable=True, ttl=60)
    now[0] = 70.0
    second, reused = pool.acquire(("credential", "one"), create=object, dispose=disposed.append)
    assert not reused and second.sandbox is not first.sandbox
    assert disposed == [first.sandbox]
    pool.close()


def test_pool_evicts_least_recently_released_idle_resource():
    """经常复用的早期会话不应只因创建较早就被优先淘汰。"""
    disposed = []
    pool = SessionSandboxPool(timer_factory=_Timer, max_idle=2)
    first, _ = pool.acquire(("credential", "one"), create=object, dispose=disposed.append)
    pool.release(first, reusable=True, ttl=60)
    second, _ = pool.acquire(("credential", "two"), create=object, dispose=disposed.append)
    pool.release(second, reusable=True, ttl=60)
    first, _ = pool.acquire(("credential", "one"), create=object, dispose=disposed.append)
    pool.release(first, reusable=True, ttl=60)
    third, _ = pool.acquire(("credential", "three"), create=object, dispose=disposed.append)
    pool.release(third, reusable=True, ttl=60)
    assert disposed == [second.sandbox]
    pool.close()
