import queue
import time
import uuid
from queue import Queue
from typing import Generator
from uuid import UUID

from redis import Redis

from internal.core.agent_v2.agents.models import AgentThought, QueueEvent
from internal.entity.conversation_entity import InvokeFrom


class AgentQueueManager:
    """管理单进程内的 Agent 事件队列。"""

    user_id: UUID
    invoke_from: InvokeFrom
    redis_client: Redis
    _queues: dict[str, Queue]

    def __init__(
        self,
        user_id: UUID,
        invoke_from: InvokeFrom,
    ) -> None:
        """初始化用户作用域和本地队列。"""
        self.user_id = user_id
        self.invoke_from = invoke_from
        self._queues = {}

        from app.http.module import injector

        self.redis_client = injector.get(Redis)

    def listen(self, task_id: UUID) -> Generator:
        """持续返回事件，并处理心跳、停止和超时。"""
        listen_timeout = 600
        start_time = time.time()
        last_ping_time = 0
        task_queue = self.queue(task_id)
        try:
            while True:
                try:
                    item = task_queue.get(timeout=1)
                except queue.Empty:
                    elapsed_time = time.time() - start_time
                    if self.is_stopped(task_id):
                        self.publish(
                            task_id,
                            AgentThought(
                                id=uuid.uuid4(),
                                task_id=task_id,
                                event=QueueEvent.STOP,
                            ),
                        )
                        continue
                    if elapsed_time >= listen_timeout:
                        self.set_stop_flag(
                            task_id,
                            self.invoke_from,
                            self.user_id,
                        )
                        self.publish(
                            task_id,
                            AgentThought(
                                id=uuid.uuid4(),
                                task_id=task_id,
                                event=QueueEvent.TIMEOUT,
                            ),
                        )
                        continue
                    if elapsed_time // 10 > last_ping_time:
                        self.publish(
                            task_id,
                            AgentThought(
                                id=uuid.uuid4(),
                                task_id=task_id,
                                event=QueueEvent.PING,
                            ),
                        )
                        last_ping_time = elapsed_time // 10
                    continue
                if item is None:
                    break
                yield item
        finally:
            self._queues.pop(str(task_id), None)

    def stop_listen(self, task_id: UUID) -> None:
        """通知监听器结束消费。"""
        task_queue = self._queues.get(str(task_id))
        if task_queue is not None:
            task_queue.put(None)

    def request_stop(self, task_id: UUID) -> None:
        """设置停止标记并唤醒当前监听器。"""
        self.set_stop_flag(task_id, self.invoke_from, self.user_id)
        task_queue = self._queues.get(str(task_id))
        if task_queue is not None:
            task_queue.put(None)

    def publish(self, task_id: UUID, agent_thought: AgentThought) -> None:
        """发布事件，并在终止事件后关闭队列。"""
        task_queue = self._queues.get(str(task_id))
        if task_queue is None:
            return
        task_queue.put(agent_thought)

        if agent_thought.event in {
            QueueEvent.STOP,
            QueueEvent.ERROR,
            QueueEvent.TIMEOUT,
            QueueEvent.AGENT_END,
        }:
            self.stop_listen(task_id)

    def is_stopped(self, task_id: UUID) -> bool:
        """检查 Redis 中的停止标记。"""
        task_stopped_cache_key = self.generate_task_stopped_cache_key(task_id)
        result = self.redis_client.get(task_stopped_cache_key)

        return result is not None

    def queue(self, task_id: UUID) -> Queue:
        """获取任务队列，缺失时按用户作用域创建。"""
        q = self._queues.get(str(task_id))

        if not q:
            user_prefix = (
                "account"
                if self.invoke_from in [InvokeFrom.WEB_APP, InvokeFrom.DEBUGGER]
                else "end-user"
            )

            self.redis_client.setex(
                self.generate_task_belong_cache_key(task_id),
                1800,
                f"{user_prefix}-{str(self.user_id)}",
            )

            q = Queue()
            self._queues[str(task_id)] = q

        return q

    @classmethod
    def set_stop_flag(
        cls, task_id: UUID, invoke_from: InvokeFrom, user_id: UUID
    ) -> None:
        """校验任务归属后写入停止标记。"""
        from app.http.module import injector

        redis_client = injector.get(Redis)

        result = redis_client.get(cls.generate_task_belong_cache_key(task_id))
        if not result:
            return

        user_prefix = (
            "account"
            if invoke_from in [InvokeFrom.WEB_APP, InvokeFrom.DEBUGGER]
            else "end-user"
        )
        if result.decode("utf-8") != f"{user_prefix}-{str(user_id)}":
            return
        stopped_cache_key = cls.generate_task_stopped_cache_key(task_id)
        redis_client.setex(stopped_cache_key, 600, 1)

    @classmethod
    def generate_task_belong_cache_key(cls, task_id: UUID) -> str:
        """生成任务归属缓存键。"""
        return f"generate_task_belong:{str(task_id)}"

    @classmethod
    def generate_task_stopped_cache_key(cls, task_id: UUID) -> str:
        """生成任务停止缓存键。"""
        return f"generate_task_stopped:{str(task_id)}"
