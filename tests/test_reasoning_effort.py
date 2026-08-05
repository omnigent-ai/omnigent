"""Unit tests for deprecated reasoning-effort alias handling.

The ChatGPT desktop app writes ``model_reasoning_effort = "ultra"`` into
``~/.codex/config.toml``; the codex CLI forwards it as the retired ``max``
wire value. Neither is accepted by the OpenAI Responses API anymore, so
``validate_effort`` coerces those aliases to ``xhigh`` — but only for
providers whose supported set doesn't already contain the raw value.
"""

from __future__ import annotations

import pytest

from omnigent.reasoning_effort import (
    ANTHROPIC_EFFORTS,
    CODEX_EFFORTS,
    EFFORT_VALUES,
    validate_effort,
)


def test_ultra_coerces_to_xhigh_for_codex() -> None:
    """The ChatGPT-app ``ultra`` maps to ``xhigh`` on the codex ladder."""
    assert validate_effort("ultra", "codex", CODEX_EFFORTS) == "xhigh"


def test_max_coerces_to_xhigh_for_codex() -> None:
    """The retired ``max`` wire value maps to ``xhigh`` on the codex ladder."""
    assert validate_effort("max", "codex", CODEX_EFFORTS) == "xhigh"


def test_ultra_coerces_for_session_metadata_vocabulary() -> None:
    """A terminal-observed ``ultra`` effort change is accepted as ``xhigh``.

    Regression: the codex-native forwarder posts the effort the codex TUI
    reports; a ChatGPT-app-configured terminal reports ``ultra``, which the
    server used to reject with ``invalid_input``.
    """
    assert validate_effort("ultra", "session metadata", EFFORT_VALUES) == "xhigh"


def test_max_stays_max_where_supported() -> None:
    """``max`` is NOT coerced for providers that genuinely support it."""
    assert validate_effort("max", "Claude Agent SDK", ANTHROPIC_EFFORTS) == "max"
    assert validate_effort("max", "session metadata", EFFORT_VALUES) == "max"


def test_unknown_effort_still_raises() -> None:
    """Values with no alias keep failing loud — no silent guessing."""
    with pytest.raises(ValueError, match="not supported"):
        validate_effort("turbo", "codex", CODEX_EFFORTS)


def test_supported_values_pass_through_unchanged() -> None:
    """In-vocabulary values are returned verbatim."""
    assert validate_effort("xhigh", "codex", CODEX_EFFORTS) == "xhigh"
    assert validate_effort("high", "codex", CODEX_EFFORTS) == "high"


def test_none_and_empty_clear_effort() -> None:
    """``None`` / empty string still mean "no explicit effort"."""
    assert validate_effort(None, "codex", CODEX_EFFORTS) is None
    assert validate_effort("", "codex", CODEX_EFFORTS) is None
