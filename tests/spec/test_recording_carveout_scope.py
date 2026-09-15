"""Recording rules distinguish CLI output from internal results."""

from pathlib import Path

_DEV = Path(__file__).resolve().parents[2] / "dev"


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_lane_rules_distinguish_cli_output_from_internal_results() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "record the real command and its output" in lanes
    assert "output is only an error message, hint, or status line" in lanes
    assert "`omnigent host` prints the wrong error after login expires" in lanes
    assert "written evidence is enough when no user interface shows the result" in lanes
    assert "an error string, a value, a log line" not in lanes


def test_lane_recording_blockers_are_explicit_and_do_not_block_delivery() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "required tool is missing" in lanes
    assert "Name the specific blocker in `recording_unavailable_reason`" in lanes
    assert "Text-only CLI output is not a reason to skip recording" in lanes
    assert "A missing recording from an earlier run is not a reason either" in lanes
    assert "Do not block the verdict, fix, or PR" in lanes


def test_repro_recording_rules_match_the_shared_guide() -> None:
    instructions = _normalized(_DEV / "repro-agent" / "AGENTS.md")

    assert (
        "For internal/API-only results with no visible user interaction, "
        "written evidence is enough" in instructions
    )
    assert (
        "record the real command and its output, even if only an error message changes"
        in instructions
    )
    assert "Text-only CLI output is not a reason to skip recording" in instructions
    assert "name the specific blocker in `recording_unavailable_reason`" in instructions
    assert "Do not block the verdict because footage is missing or rejected" in instructions
