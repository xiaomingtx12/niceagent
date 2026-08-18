"""Agent graph execution helper tests."""

from types import SimpleNamespace

from internal.core.agent_v2.agents.execution import (
    GraphExecutionDriver,
    checkpoint_has_state,
    close_runtime_backends,
)


def test_checkpoint_state_probe_is_fail_soft_for_bootstrap_only():
    """Saver probe failures return false and do not hide execution failures."""
    graph = SimpleNamespace(get_state=lambda _config: (_ for _ in ()).throw(RuntimeError()))

    assert checkpoint_has_state(graph, checkpointer=True, config={}) is False


def test_close_runtime_backends_deduplicates_shared_routes():
    """One Backend mounted on several routes closes only once."""
    backend = SimpleNamespace(close_calls=0)

    def close():
        """Record one lifecycle close."""
        backend.close_calls += 1

    backend.close = close
    composite = SimpleNamespace(
        default=backend,
        routes={"/workspace/": backend, "/artifacts/": backend},
    )

    close_runtime_backends(composite)

    assert backend.close_calls == 1


def test_graph_execution_stops_consuming_after_cancellation():
    """停止标记出现后不再消费后续图更新。"""
    consumed = []
    closed = SimpleNamespace(close_calls=0)

    class Graph:
        def stream(self, *_args, **_kwargs):
            consumed.append(1)
            yield {"step": 1}
            consumed.append(2)
            yield {"step": 2}

    def close():
        """记录 Backend 释放。"""
        closed.close_calls += 1

    closed.close = close
    GraphExecutionDriver(
        graph=Graph(),
        publisher=lambda *_args: None,
        backend=SimpleNamespace(default=closed, routes={}),
        cancelled=lambda: True,
    ).run(
        input_state={},
        task_id=__import__("uuid").uuid4(),
        config=None,
        stream_options={},
    )

    assert consumed == [1]
    assert closed.close_calls == 1
