"""Instructions tests for Codex forwarder."""

from __future__ import annotations

from omnigent.harnesses.codex_native import forwarder as fwd


def test_default_collaboration_mode_requires_model() -> None:
    """Without a confirmed model there is no payload to send."""
    state = fwd._CodexForwarderState()

    assert fwd._default_collaboration_mode(state) is None


def test_default_collaboration_mode_never_forwards_developer_instructions() -> None:
    """``settings.developer_instructions`` stays ``null``.

    A non-null value replaces the mode's built-in instructions instead of
    adding to them; the agent's authored instructions already reach every turn
    through the additive top-level ``developer_instructions`` config key.
    """
    state = fwd._CodexForwarderState()
    state.model = "gpt-5.4"

    assert fwd._default_collaboration_mode(state) == {
        "mode": "default",
        "settings": {
            "model": "gpt-5.4",
            "reasoning_effort": None,
            "developer_instructions": None,
        },
    }
