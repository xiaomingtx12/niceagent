"""将 AgentThought 聚合为非流式 AgentResult。"""

from __future__ import annotations

from internal.core.agent_v2.agents.models import (
    AgentResult,
    AgentThought,
    QueueEvent,
)


class AgentResultAccumulator:
    """把流式业务事件聚合为稳定结果。"""

    def __init__(self, *, query: str, image_urls: list[str]) -> None:
        """为当前输入初始化空结果。"""
        self._result = AgentResult(query=query, image_urls=image_urls)
        self._thoughts: dict[str, AgentThought] = {}

    def add(self, thought: AgentThought) -> None:
        """合并一个流式事件。"""
        if thought.event == QueueEvent.PING:
            return
        event_id = str(thought.id)
        if thought.event == QueueEvent.AGENT_MESSAGE and event_id in self._thoughts:
            self._thoughts[event_id] = _merge_agent_message(
                self._thoughts[event_id], thought
            )
        else:
            self._thoughts[event_id] = thought

        if thought.event == QueueEvent.AGENT_MESSAGE:
            self._result.answer += thought.answer
        elif thought.event in {QueueEvent.STOP, QueueEvent.TIMEOUT, QueueEvent.ERROR}:
            self._result.status = thought.event
            self._result.error = (
                thought.observation if thought.event == QueueEvent.ERROR else ""
            )

    def build(self) -> AgentResult:
        """整理推理顺序、消息和总耗时。"""
        thoughts = list(self._thoughts.values())
        self._result.agent_thoughts = thoughts
        self._result.message = next(
            (
                thought.message
                for thought in thoughts
                if thought.event == QueueEvent.AGENT_MESSAGE
            ),
            [],
        )
        self._result.message_token_count = sum(
            thought.message_token_count for thought in thoughts
        )
        self._result.answer_token_count = sum(
            thought.answer_token_count for thought in thoughts
        )
        self._result.total_token_count = sum(
            thought.total_token_count for thought in thoughts
        )
        self._result.total_price = sum(thought.total_price for thought in thoughts)
        self._result.latency = sum(thought.latency for thought in thoughts)
        return self._result


def _merge_agent_message(previous: AgentThought, current: AgentThought) -> AgentThought:
    """合并同一 Agent 消息事件的重复分片。"""
    return previous.model_copy(
        update={
            "thought": previous.thought + current.thought,
            "message": current.message,
            "message_token_count": current.message_token_count,
            "message_unit_price": current.message_unit_price,
            "message_price_unit": current.message_price_unit,
            "answer": previous.answer + current.answer,
            "answer_token_count": current.answer_token_count,
            "answer_unit_price": current.answer_unit_price,
            "answer_price_unit": current.answer_price_unit,
            "total_token_count": current.total_token_count,
            "total_price": current.total_price,
            "latency": current.latency,
        }
    )


__all__ = ["AgentResultAccumulator"]
