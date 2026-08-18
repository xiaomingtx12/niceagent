"""Graph 执行与单次运行的 Backend 生命周期。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from deepagents.backends.protocol import BackendProtocol
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from internal.core.agent_v2.agents.models import AgentThought
from internal.core.agent_v2.runtime.models import AgentRunContext
from internal.core.agent_v2.events.session import bind_event_translation
from internal.core.agent_v2.runtime.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)


@dataclass
class GraphExecutionDriver:
    """驱动一次图流并协调运行结束与资源清理。"""

    graph: CompiledStateGraph
    publisher: Callable[[UUID, AgentThought], None]
    runtime_context: AgentRunContext
    telemetry: AgentTelemetry = NOOP_AGENT_TELEMETRY
    cancelled: Callable[[], bool] = lambda: False
    on_cleanup: Callable[[], None] | None = None

    def run(
        self,
        *,
        input_state: dict[str, Any],
        task_id: UUID,
        config: RunnableConfig | None,
        stream_options: dict[str, Any],
    ) -> None:
        """消费图输出，由中间件发布业务事件。"""
        with bind_event_translation(
            task_id=task_id,
            publisher=self.publisher,
            telemetry=self.telemetry,
            cancelled=self.cancelled,
        ) as event_session:
            try:
                for _ in self.graph.stream(
                    input_state,
                    config=config,
                    context=self.runtime_context,
                    **stream_options,
                ):
                    if self.cancelled():
                        event_session.cancel()
                        break
            except Exception as exc:  # noqa: BLE001
                event_session.publish_error(exc)
            finally:
                # Backend 关闭可能同步 Workspace 或回收 Sandbox。必须先
                # 完成清理，再发布 AGENT_END，避免前端先收到成功事件。
                if self.on_cleanup is not None:
                    try:
                        self.on_cleanup()
                    except Exception as exc:  # noqa: BLE001
                        event_session.publish_error(exc)
                event_session.finish()


def close_runtime_backends(backend: BackendProtocol | None) -> None:
    """逐个关闭运行时持有的 Backend，且避免重复关闭。"""
    if backend is None:
        return
    candidates = [backend, getattr(backend, "default", None)]
    routes = getattr(backend, "routes", {}) or {}
    candidates.extend(routes.values())
    seen: set[int] = set()
    errors: list[Exception] = []
    for candidate in candidates:
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        close = getattr(candidate, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
    if errors:
        raise RuntimeError(
            f"failed to close {len(errors)} runtime backend(s)"
        ) from errors[0]


__all__ = [
    "GraphExecutionDriver",
    "close_runtime_backends",
]
