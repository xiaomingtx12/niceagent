"""请求 State 与 DeepAgents 图 State 的边界测试。"""

from uuid import UUID, uuid4

from internal.core.agent_v2.state.bootstrap import (
    apply_history_bootstrap,
    prepare_request_state,
    strip_transport_state,
)


def test_prepare_request_state_copies_transport_fields_without_mutating_input():
    task_id = uuid4()
    original = {
        "messages": ["current"],
        "history": ["previous"],
        "task_id": task_id,
        "iteration_count": 3,
    }

    prepared = prepare_request_state(original)

    assert prepared == original
    assert prepared["messages"] is not original["messages"]
    assert prepared["history"] is not original["history"]


def test_apply_history_bootstrap_only_uses_history_on_cold_start():
    state = {
        "messages": ["current"],
        "history": ["previous"],
        "task_id": uuid4(),
        "iteration_count": 2,
    }

    cold_start = apply_history_bootstrap(state, has_checkpoint=False)
    resumed = apply_history_bootstrap(state, has_checkpoint=True)

    assert cold_start["messages"] == ["previous", "current"]
    assert resumed["messages"] == ["current"]
    assert "history" not in cold_start
    assert "history" not in resumed


def test_strip_transport_state_keeps_only_graph_input_and_task_id():
    task_id = uuid4()
    graph_input, extracted_task_id = strip_transport_state(
        {
            "messages": ["current"],
            "task_id": task_id,
            "iteration_count": 7,
        }
    )

    assert graph_input == {"messages": ["current"]}
    assert extracted_task_id == task_id


def test_strip_transport_state_generates_task_id_when_missing():
    graph_input, task_id = strip_transport_state({"messages": []})

    assert graph_input == {"messages": []}
    assert isinstance(task_id, UUID)
