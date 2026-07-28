"""Tests for the native Harness Profile safety adapter."""

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from internal.core.agent_v2.harness_profile import (
    HarnessProfileError,
    disable_implicit_general_purpose,
)


def test_default_general_purpose_is_disabled_for_prebuilt_model():
    from deepagents.profiles.harness.harness_profiles import (
        _harness_profile_for_model,
    )

    model = FakeListChatModel(responses=["ok"])
    key = disable_implicit_general_purpose(model)
    profile = _harness_profile_for_model(model, None)

    assert key == "fakelistchatmodel"
    assert profile.general_purpose_subagent is not None
    assert profile.general_purpose_subagent.enabled is False


def test_unidentifiable_model_fails_closed(monkeypatch):
    model = FakeListChatModel(responses=["ok"])
    monkeypatch.setattr(model, "_get_ls_params", lambda: {})

    with pytest.raises(HarnessProfileError, match="不能安全关闭"):
        disable_implicit_general_purpose(model)

