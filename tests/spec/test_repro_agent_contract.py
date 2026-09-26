"""The already_fixed verdict requires a later fix and observed corrected behavior."""

from pathlib import Path

_REPRO_AGENT_INSTRUCTIONS = (
    Path(__file__).resolve().parents[2] / "dev" / "repro-agent" / "AGENTS.md"
)


def _normalized_instructions() -> str:
    return " ".join(_REPRO_AGENT_INSTRUCTIONS.read_text(encoding="utf-8").split())


def test_already_fixed_requires_fix_to_postdate_report() -> None:
    """A fix already deployed when a bug was reported cannot explain the report."""
    normalized = _normalized_instructions()

    assert "newer than the report" in normalized
    assert "already live when the user complained" in normalized


def test_already_fixed_requires_live_observation_of_corrected_behaviour() -> None:
    """An unobserved candidate fix requires manual review."""
    normalized = _normalized_instructions()

    assert "observed the corrected behaviour live" in normalized
    assert "cite the candidate fix as a lead" in normalized


def test_recording_unavailable_reason_cannot_excuse_an_unobserved_fix() -> None:
    """The proof-it-works clip gate must not be bypassable by a reason string."""
    normalized = _normalized_instructions()

    assert "never substitutes for observing the behaviour" in normalized
