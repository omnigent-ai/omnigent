"""Tests for the shared native startup-additive raw-instruction resolution.

``_native_startup_raw_instructions_from_spec`` is the seam that turns a
session's ``AgentSpec.instructions`` into the value passed to the
managed-host launch paths' startup-additive channels: claude-native's
``--append-system-prompt`` (``_auto_create_claude_terminal``) and
codex-native's ``developer_instructions`` (``_auto_create_codex_terminal``).
It must return the verbatim author text only — never the framework-composed
per-turn string. Terminal launch is not tied to any one turn, while the
composed string is assembled per conversation for the turn about to run, so
carrying one turn's composition would address every later turn with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runner.app import ResolvedSpec, _native_startup_raw_instructions_from_spec
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _spec(instructions: str | None) -> AgentSpec:
    """Build a minimal agent spec carrying *instructions*."""
    return AgentSpec(
        spec_version=1,
        name="claude_code",
        instructions=instructions,
        executor=ExecutorSpec(),
    )


@pytest.mark.parametrize(
    ("instructions", "expected"),
    [
        ("Be a concise assistant.", "Be a concise assistant."),
        (None, None),
        ("   \n  ", None),
    ],
    ids=["present", "absent", "whitespace-only"],
)
def test_native_startup_raw_instructions_from_spec(
    instructions: str | None, expected: str | None
) -> None:
    """Verbatim author text is returned; absent/whitespace-only resolves to None."""
    assert _native_startup_raw_instructions_from_spec(_spec(instructions)) == expected


def test_native_startup_raw_instructions_from_spec_none_spec() -> None:
    """A missing spec yields no instructions (neither channel is injected)."""
    assert _native_startup_raw_instructions_from_spec(None) is None


def test_native_startup_raw_instructions_from_spec_resolved_wrapper() -> None:
    """A ResolvedSpec wrapper unwraps to the same instructions text."""
    wrapped = ResolvedSpec(spec=_spec("Be a concise assistant."), workdir=Path("/tmp"))
    assert _native_startup_raw_instructions_from_spec(wrapped) == "Be a concise assistant."


def test_native_startup_raw_instructions_from_spec_never_returns_stripped_form() -> None:
    """The original resolved text is preserved verbatim, not the stripped form."""
    padded = "  Keep leading/trailing whitespace exactly.  "
    assert _native_startup_raw_instructions_from_spec(_spec(padded)) == padded
