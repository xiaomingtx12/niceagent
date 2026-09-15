"""Run 事件中心：内存负责低延迟交付，数据库负责长期历史。"""

from collections import deque
from threading import Condition

from internal.schema.agent.agent_protocol_schema import AgentProtocolEvent


class AgentExecutionEventHub:
    """封装实时事件尾部和等待通知，调用方不需要分别管理队列与条件变量。"""

    # 大于写入队列上限；正常执行时，被淘汰的事件必定已经提交。
    CAPACITY = 2048

    def __init__(self, initial_sequence: int) -> None:
        """每个执行片段独占缓冲，恢复表单后从已提交序号重新开始。"""
        self._events: deque[AgentProtocolEvent] = deque(maxlen=self.CAPACITY)
        self._initial_sequence = initial_sequence
        self._condition = Condition()
        self._version = 0
        self._closed = False

    def publish(self, events: list[AgentProtocolEvent]) -> None:
        """事件对象发布后不再修改；多个订阅者共享载荷，不复制整段正文。"""
        with self._condition:
            if self._closed:
                return
            self._events.extend(events)
            self._version += 1
            self._condition.notify_all()

    def read_after(self, sequence: int, limit: int) -> list[AgentProtocolEvent] | None:
        """None 表示历史缺口；空列表表示已追上当前内存尾部。"""
        with self._condition:
            oldest_cursor = (
                self._events[0].seq - 1 if self._events else self._initial_sequence
            )
            if sequence < oldest_cursor:
                return None
            if not self._events or sequence >= self._events[-1].seq:
                return []
            result: deque[AgentProtocolEvent] = deque(maxlen=limit)
            # 从尾部扫到游标，只保留最早一批未读事件，积压时也不复制整个尾部。
            for event in reversed(self._events):
                if event.seq <= sequence:
                    break
                result.appendleft(event)
            return list(result)

    def change_token(self) -> int:
        """返回当前变化版本；版本只用于判断是否需要重新读取事件。"""
        with self._condition:
            return self._version

    def wait_for_change(self, version: int, *, timeout: float) -> None:
        """等待事件发布或 Run 关闭；超时由 SSE 心跳机制控制。"""
        with self._condition:
            self._condition.wait_for(
                lambda: self._version != version or self._closed,
                timeout=timeout,
            )

    def close(self) -> None:
        """关闭订阅并唤醒等待者，事件正文仍由数据库保留。"""
        with self._condition:
            self._closed = True
            self._version += 1
            self._condition.notify_all()


__all__ = ["AgentExecutionEventHub"]
