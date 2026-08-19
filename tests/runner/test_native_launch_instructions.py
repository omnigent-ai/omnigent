"""Focused coverage for raw author instructions used by native launches.

The shared resolver feeds claude-native's ``--append-system-prompt`` and
codex-native's ``developer_instructions`` launch channels. Composition with
framework-owned routing notes is covered at those launch boundaries.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runner.native.orchestration import (
    ResolvedSpec,
    _native_startup_raw_instructions_from_spec,
)
from omnigent.spec.types import AgentSpec

_INSTRUCTIONS = "# Ferrous Sparrow Protocol\n\nAlways greet in Latin."


def test_native_startup_instructions_are_read_from_a_bare_agent_spec() -> None:
    """Resolved bundle instructions are read directly from ``AgentSpec``."""
    spec = AgentSpec(spec_version=1, name="sparrow", instructions=_INSTRUCTIONS)

    assert _native_startup_raw_instructions_from_spec(spec) == _INSTRUCTIONS


def test_native_startup_instructions_are_read_through_a_resolved_spec_wrapper() -> None:
    """Managed launch sites may receive a ``ResolvedSpec`` wrapper."""
    spec = AgentSpec(spec_version=1, name="sparrow", instructions=_INSTRUCTIONS)
    resolved = ResolvedSpec(spec=spec, workdir=Path("/tmp"))

    assert _native_startup_raw_instructions_from_spec(resolved) == _INSTRUCTIONS


@pytest.mark.parametrize(
    "agent_spec",
    [
        None,
        AgentSpec(spec_version=1, name="sparrow"),
        AgentSpec(spec_version=1, name="sparrow", instructions="  \n\t "),
    ],
    ids=["missing-spec", "missing-instructions", "blank-instructions"],
)
def test_missing_or_blank_native_startup_instructions_contribute_nothing(
    agent_spec: AgentSpec | None,
) -> None:
    """Empty inputs do not cause either native launch channel to be emitted."""
    assert _native_startup_raw_instructions_from_spec(agent_spec) is None


def test_native_startup_instructions_preserve_author_whitespace() -> None:
    """Nonblank author text remains byte-identical for the launch channel."""
    instructions = f"  {_INSTRUCTIONS}  "
    spec = AgentSpec(spec_version=1, name="sparrow", instructions=instructions)

    assert _native_startup_raw_instructions_from_spec(spec) == instructions
