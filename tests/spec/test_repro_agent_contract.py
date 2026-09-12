"""Contract tests for the repro-agent instructions.

The repro agent's verdicts drive automated Linear write-backs, so the
instruction file is load-bearing: a wording gap becomes a wrong
`validated:*` label. These tests pin the `already_fixed` preconditions —
a cited fix must postdate the report, and the corrected behaviour must
have been observed live — so a rewrite cannot silently drop them.
"""

from pathlib import Path

_REPRO_AGENT_INSTRUCTIONS = (
    Path(__file__).resolve().parents[2] / "dev" / "repro-agent" / "AGENTS.md"
)


def _normalized_instructions() -> str:
    return " ".join(_REPRO_AGENT_INSTRUCTIONS.read_text(encoding="utf-8").split())


def test_already_fixed_requires_fix_to_postdate_report() -> None:
    """A fix already live when the user complained cannot explain the report.

    Without this precondition the agent can cite any older commit as "the
    fix" and ship `already_fixed` for behaviour the user reported while
    that commit was already deployed.
    """
    normalized = _normalized_instructions()

    assert "newer than the report" in normalized
    assert "already live when the user complained" in normalized


def test_already_fixed_requires_live_observation_of_corrected_behaviour() -> None:
    """`already_fixed` is an observation, not an inference from `git log`.

    When the surface cannot be driven, the verdict defers to a human
    (`needs_manual_review`) with the candidate fix as a lead, instead of
    declaring fixed a behaviour nobody watched.
    """
    normalized = _normalized_instructions()

    assert "observed the corrected behaviour live" in normalized
    assert "cite the candidate fix as a lead" in normalized


def test_recording_unavailable_reason_cannot_excuse_an_unobserved_fix() -> None:
    """The proof-it-works clip gate must not be bypassable by a reason string."""
    normalized = _normalized_instructions()

    assert "never substitutes for observing the behaviour" in normalized
