"""Recording rules distinguish CLI output from internal results and keep captions honest."""

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


def test_finishing_a_clip_requires_frame_inspection_or_a_disclosed_dom_only_caption() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "Check the clip's last frame before captioning it" in lanes
    assert "Look at the frame, not at a proxy for it" in lanes
    assert "ffmpeg -i <clip> -update 1 last-frame.png" in lanes
    assert "keeps the last decoded frame" in lanes
    assert "-frames:v 1 -update 1 last-frame.png" not in lanes
    assert "Pixel-colour counts, file sizes, or the test's own intent" in lanes
    assert "If the frames cannot be inspected" in lanes
    assert "caption only the state the driver's DOM assertions established" in lanes
    assert "frames not inspected; state verified by DOM assertions" in lanes
    assert "Do not describe a screen nobody verified" in lanes


def test_before_clip_of_an_absence_must_rule_out_a_different_failure() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "must rule out a different failure" in lanes
    assert "will also pass on a session that failed for an unrelated reason" in lanes
    assert "assert that no generic error notice is on screen" in lanes
    assert (
        'page.locator(\'[data-testid="error-pill"][data-level="error"]\')).to_have_count(0)'
        in lanes
    )
    assert "the stall context the caption describes is visible" in lanes
    assert "demonstrates that error, not a silent stall" in lanes


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
