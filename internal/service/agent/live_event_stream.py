"""活动执行的短期事件尾部；数据库保存历史，内存只负责低延迟交付。"""

from collections import deque
from threading import Lock

from internal.schema.agent.agent_protocol_schema import AgentProtocolEvent


class LiveEventStream:
    """按协议序号补接实时事件；落后于保留范围的订阅者回数据库追平。"""

    # 大于写入队列上限；正常执行时，被淘汰的事件必定已经提交。
    CAPACITY = 2048

    def __init__(self, initial_sequence: int) -> None:
        """每个执行片段独占缓冲，恢复表单后从已提交序号重新开始。"""
        self._events: deque[AgentProtocolEvent] = deque(maxlen=self.CAPACITY)
        self._initial_sequence = initial_sequence
        self._lock = Lock()

    def publish(self, events: list[AgentProtocolEvent]) -> None:
        """事件对象发布后不再修改；多个订阅者共享载荷，不复制整段正文。"""
        with self._lock:
            self._events.extend(events)

    def read_after(self, sequence: int, limit: int) -> list[AgentProtocolEvent] | None:
        """None 表示历史缺口；空列表表示已追上当前内存尾部。"""
        with self._lock:
            oldest_cursor = self._events[0].seq - 1 if self._events else self._initial_sequence
            if sequence < oldest_cursor:
                return None
            result = []
            for event in self._events:
                if event.seq > sequence:
                    result.append(event)
                    if len(result) >= limit:
                        break
            return result
