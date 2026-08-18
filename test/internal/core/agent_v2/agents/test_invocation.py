"""Agent invocation input preparation tests."""

from langchain_core.messages import AIMessage, HumanMessage

from internal.core.agent_v2.agents.invocation import (
    apply_history_bootstrap,
    extract_query_and_images,
    prepare_stream_input,
)


def test_extract_query_uses_latest_human_message_and_images():
    """Input extraction ignores prior AI messages and preserves image URLs."""
    query, images = extract_query_and_images(
        {
            "messages": [
                HumanMessage(content="旧问题"),
                AIMessage(content="旧回答"),
                HumanMessage(
                    content=[
                        {"type": "text", "text": "当前"},
                        {"type": "text", "text": "问题"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/a.png"},
                        },
                    ]
                ),
            ]
        }
    )

    assert query == "当前问题"
    assert images == ["https://example.com/a.png"]


def test_history_bootstrap_does_not_mutate_caller_input():
    """Checkpoint bootstrap chooses one history source on a copied input."""
    original = {
        "history": [HumanMessage(content="历史")],
        "messages": [HumanMessage(content="当前")],
    }

    prepared = prepare_stream_input(original)
    first_run = apply_history_bootstrap(prepared, has_checkpoint=False)
    resumed = apply_history_bootstrap(prepared, has_checkpoint=True)

    assert len(first_run["messages"]) == 2
    assert len(resumed["messages"]) == 1
    assert original["messages"][0].content == "当前"
    assert "task_id" not in original

