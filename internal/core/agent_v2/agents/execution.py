"""Graph 执行与单次运行的 Backend 生命周期。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from deepagents.backends.protocol import BackendProtocol
from langchain_core.callbacks import UsageMetadataCallbackHandler
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from internal.core.agent_v2.agents.models import AgentThought
from internal.core.agent_v2.runtime.models import AgentRunContext
from internal.core.agent_v2.events.session import bind_event_translation
from internal.core.agent_v2.events.translator import (
    translate_graph_update,
    translate_model_chunk,
)
from internal.core.agent_v2.runtime.telemetry import (
    AgentTelemetry,
    NOOP_AGENT_TELEMETRY,
)


@dataclass
class GraphExecutionDriver:
    """驱动一次图流并协调运行结束与资源清理。"""

    graph: CompiledStateGraph
    publisher: Callable[[UUID, AgentThought], None]
    runtime_context: AgentRunContext | None = None
    telemetry: AgentTelemetry = NOOP_AGENT_TELEMETRY
    cancelled: Callable[[], bool] = lambda: False
    on_cleanup: Callable[[], None] | None = None
    on_usage: Callable[[dict[str, Any]], None] | None = None
    backend: BackendProtocol | None = None

    def run(
        self,
        *,
        input_state: dict[str, Any],
        task_id: UUID,
        config: RunnableConfig | None,
        stream_options: dict[str, Any],
    ) -> None:
        """消费图输出，由中间件发布业务事件。"""
        usage_callback = UsageMetadataCallbackHandler()
        effective_config = append_usage_callback(config, usage_callback)
        with bind_event_translation(
            task_id=task_id,
            publisher=self.publisher,
            telemetry=self.telemetry,
            cancelled=self.cancelled,
            stream_model_messages=True,
        ) as event_session:
            try:
                stream_options = dict(stream_options)
                stream_options.setdefault(
                    "stream_mode",
                    ["messages", "updates"],
                )
                for item in self.graph.stream(
                    input_state,
                    config=effective_config,
                    context=self.runtime_context,
                    **stream_options,
                ):
                    for thought in _translate_stream_item(event_session, item):
                        event_session.publish(thought)
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
                elif self.backend is not None:
                    try:
                        close_runtime_backends(self.backend)
                    except Exception as exc:  # noqa: BLE001
                        event_session.publish_error(exc)
                usage_payload = event_session.publish_token_usage(
                    usage_callback.usage_metadata,
                )
                if usage_payload is not None and self.on_usage is not None:
                    try:
                        self.on_usage(usage_payload)
                    except Exception:  # noqa: BLE001
                        self.telemetry.emit(
                            "agent.run_usage_persistence.failed",
                            attributes={"task_id": str(task_id)},
                        )
                event_session.finish()


def _translate_stream_item(
    event_session,
    item: Any,
) -> list[AgentThought]:
    """翻译 DeepAgents 0.6 的 messages/updates 流事件。"""
    if not isinstance(item, tuple):
        return []

    if len(item) == 2:
        mode, payload = item
    elif len(item) == 3:
        _, mode, payload = item
    else:
        return []

    if mode == "messages":
        if not isinstance(payload, tuple) or not payload:
            return []
        message = payload[0]
        metadata = payload[1] if len(payload) > 1 else None
        thought = translate_model_chunk(
            event_session,
            message,
            metadata if isinstance(metadata, Mapping) else None,
        )
        return [thought] if thought is not None else []

    if mode == "updates" and isinstance(payload, Mapping):
        return translate_graph_update(event_session, payload)
    return []


def append_usage_callback(
    config: RunnableConfig | None,
    callback: UsageMetadataCallbackHandler,
) -> RunnableConfig:
    """附加当前运行专用的用量聚合器，不修改调用方配置。"""
    effective: RunnableConfig = dict(config or {})
    existing = effective.get("callbacks")
    if existing is None:
        callbacks = []
    elif isinstance(existing, list):
        callbacks = list(existing)
    else:
        callbacks = [existing]
    callbacks.append(callback)
    effective["callbacks"] = callbacks
    return effective


def checkpoint_has_state(
    graph: CompiledStateGraph,
    *,
    checkpointer: Any,
    config: RunnableConfig | None,
) -> bool:
    """检查已有 checkpoint，失败时按无状态处理。"""
    if not checkpointer or config is None:
        return False
    try:
        state = graph.get_state(config)
    except Exception:  # noqa: BLE001
        return False
    values = getattr(state, "values", state)
    if isinstance(values, Mapping):
        return bool(values.get("messages"))
    return False


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
    "append_usage_callback",
    "checkpoint_has_state",
    "close_runtime_backends",
]
