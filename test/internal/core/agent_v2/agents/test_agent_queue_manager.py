"""Agent 事件队列生命周期测试。"""

import queue
from uuid import uuid4

from internal.core.agent_v2.agents.agent_queue_manager import AgentQueueManager
from internal.core.agent_v2.entities.queue_entity import QueueEvent
from internal.entity.conversation_entity import InvokeFrom


class _ImmediateQueue:
    """无需等待即可触发空队列分支。"""

    def __init__(self):
        self.items = []

    def get(self, timeout=None):
        """返回下一项，缺失时立即抛出空队列异常。"""
        if not self.items:
            raise queue.Empty
        return self.items.pop(0)

    def put(self, item):
        """追加一项。"""
        self.items.append(item)


def test_stop_event_is_emitted_once_and_local_queue_is_released():
    """停止标记只产生一个终止事件。"""
    task_id = uuid4()
    task_queue = _ImmediateQueue()
    manager = AgentQueueManager.__new__(AgentQueueManager)
    manager.user_id = uuid4()
    manager.invoke_from = InvokeFrom.DEBUGGER
    manager.redis_client = type(
        "StoppedRedis",
        (),
        {"get": lambda _self, _key: b"1"},
    )()
    manager._queues = {str(task_id): task_queue}

    events = list(manager.listen(task_id))

    assert [event.event for event in events] == [QueueEvent.STOP]
    assert str(task_id) not in manager._queues
