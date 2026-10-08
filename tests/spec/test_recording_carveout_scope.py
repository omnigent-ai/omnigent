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


def test_lane_screen_claims_come_from_frames_not_the_expected_surface() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "What the caption says is or is not on screen must come from the frames" in lanes
    assert "which program the pane shows" in lanes
    assert 'equally the negative: "not drawn", "not legible", "no overlay"' in lanes
    assert "with no image viewer, OCR it" in lanes
    assert "the attach WebSocket or PTY byte stream are not the screen" in lanes
    assert "dropped, not flipped into a negative" in lanes
    assert (
        "`recording_unavailable_reason`, `evidence`, the PR body, and the live-validation prompt"
        in lanes
    )


def test_lane_terminal_dumps_are_the_screen_read_when_frames_cannot_be_viewed() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")
    terminal = lanes.split("## `terminal` facets", 1)[1].split("## `cli` facets", 1)[0]

    assert "they hold what the pane draws" in terminal
    assert "check a caption's screen claims when you cannot view a frame" in terminal
    assert "The attach WebSocket or PTY byte stream is not the screen" in terminal
